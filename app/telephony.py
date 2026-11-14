"""Phone calls through Asterisk (ARI): agents call people, people call agents.

How a call works
  outbound: POST /api/calls → ARI originate (channel id = call id) → callee answers → StasisStart
  inbound:  dialplan `Stasis(agentchat,in,<agent>)` → StasisStart → answer
  then:     speak (TTS → 8 kHz WAV → ARI play) ⇄ listen (ARI record → STT) ⇄ think (agent's Ollama brain)
Every line of the call is posted live as a thread on the agent's feed, so you can read along.

Speech uses OpenAI-compatible endpoints, so it stays self-hosted:
  TTS  POST {tts_url}/audio/speech          (Speaches/Kokoro/Piper, openedai-speech, LocalAI…)
  STT  POST {stt_url}/audio/transcriptions  (Speaches/faster-whisper, whisper.cpp server, LocalAI…)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException

log = logging.getLogger("agentchat.phone")
BYE = re.compile(r"\b(bye|goodbye|good bye|hang up|that'?s all|adi[oó]s|chao|ciao)\b", re.I)
PHONE_GUARD = ("You are speaking on a live phone call. Reply in one or two short, natural spoken sentences. "
               "No markdown, no emoji, no lists, no URLs, no code. If the caller says goodbye, say a brief goodbye.")


class Call:
    def __init__(self, agent: dict, direction: str, number: str, message: str = "", conversation: bool = False,
                 max_turns: int = 8, reason: str = "", by: str = "") -> None:
        self.id = "call-" + secrets.token_hex(6)
        self.agent, self.direction, self.number = agent, direction, number
        self.message, self.conversation, self.max_turns, self.reason, self.by = message, conversation, max_turns, reason, by
        self.channel: Optional[str] = None
        self.state = "dialing"
        self.started, self.answered, self.ended = time.time(), None, None
        self.turns, self.root_id, self.error, self.hungup = 0, None, None, False
        self.events: asyncio.Queue = asyncio.Queue()

    def public(self) -> dict:
        end = self.ended or time.time()
        return {"id": self.id, "agent": self.agent["name"], "direction": self.direction, "number": self.number,
                "state": self.state, "reason": self.reason, "conversation": self.conversation, "turns": self.turns,
                "started": self.started, "answered": self.answered, "ended": self.ended, "by": self.by,
                "duration": round(end - self.answered) if self.answered else 0, "thread": self.root_id, "error": self.error}

    async def wait_for(self, pred, timeout: float) -> Optional[dict]:
        deadline = time.time() + timeout
        while not self.hungup:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                ev = await asyncio.wait_for(self.events.get(), remaining)
            except asyncio.TimeoutError:
                return None
            if ev.get("type") in ("StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"):
                self.hungup = True
                return None
            if pred(ev):
                return ev
        return None


class Telephony:
    def __init__(self, net) -> None:
        self.net, self.s = net, net.s
        self.calls: "OrderedDict[str, Call]" = OrderedDict()
        self.by_channel: dict[str, str] = {}
        self.media: "OrderedDict[str, bytes]" = OrderedDict()
        self.rate: dict[str, deque] = defaultdict(deque)
        self.last_escalation: dict[str, float] = {}
        self.connected = False
        self.ffmpeg = shutil.which("ffmpeg")

    # ---------------------------------------------------------------- capability flags
    @property
    def enabled(self) -> bool:
        return bool(self.s.ari_url)

    @property
    def can_speak(self) -> bool:
        return bool(self.s.tts_url)

    @property
    def can_listen(self) -> bool:
        return bool(self.s.stt_url) and self.net.llm.configured

    def status(self) -> dict:
        return {"enabled": self.enabled, "connected": self.connected, "tts": self.can_speak, "stt": bool(self.s.stt_url),
                "conversation": self.can_listen, "allow": self.s.call_allow, "oncall": bool(self.s.oncall_number),
                "ari_app": self.s.ari_app}

    # ---------------------------------------------------------------- ARI plumbing
    async def ari(self, method: str, path: str, **kw) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20, auth=(self.s.ari_user, self.s.ari_password)) as c:
            r = await c.request(method, self.s.ari_url.rstrip("/") + path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"ARI {method} {path} → {r.status_code} {r.text[:200]}")
        return r

    async def run(self) -> None:
        import websockets
        base = re.sub(r"^http", "ws", self.s.ari_url.rstrip("/"))
        url = f"{base}/events?app={quote(self.s.ari_app)}&api_key={quote(self.s.ari_user)}:{quote(self.s.ari_password)}"
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
                    self.connected = True
                    log.info("connected to Asterisk ARI app '%s'", self.s.ari_app)
                    async for raw in ws:
                        try:
                            await self.dispatch(json.loads(raw))
                        except Exception:
                            log.exception("ARI event handling failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.connected:
                    log.warning("ARI connection lost: %s", e)
                else:
                    log.debug("ARI connect failed: %s", e)
            self.connected = False
            await asyncio.sleep(5)

    async def dispatch(self, ev: dict) -> None:
        t = ev.get("type")
        ch = ev.get("channel") or {}
        cid = ch.get("id")
        if t == "StasisStart":
            args = ev.get("args") or []
            if args[:1] == ["out"] and len(args) > 1 and args[1] in self.calls:
                call = self.calls[args[1]]
                call.channel = cid
                self.by_channel[cid] = call.id
                self.net.spawn(self.converse(call))
            elif args[:1] == ["in"]:
                await self.incoming(cid, args[1] if len(args) > 1 else self.s.phone_default_agent,
                                    (ch.get("caller") or {}).get("number") or "unknown")
            return
        for key in ("playback", "recording"):
            if key in ev and not cid:
                target = ev[key].get("target_uri", "")
                cid = target.split(":", 1)[1] if target.startswith("channel:") else None
        call = self.calls.get(self.by_channel.get(cid or "", ""))
        if not call:
            return
        if t in ("ChannelDestroyed", "StasisEnd") and call.state == "dialing":
            call.state, call.hungup = "no-answer", True
            call.error = ev.get("cause_txt") or "not answered"
            await self.line(call, "system", f"📵 No answer ({call.error})")
            await self.finish(call)
            return
        if t in ("StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"):
            call.hungup = True
        await call.events.put(ev)

    # ---------------------------------------------------------------- speech
    async def to_8k(self, data: bytes) -> bytes:
        if not self.ffmpeg:
            return data
        p = await asyncio.create_subprocess_exec(self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                                                 "-ar", "8000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
                                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        out, _ = await p.communicate(data)
        return out or data

    async def tts(self, text: str, agent: dict) -> bytes:
        voice = json.loads(agent.get("voice") or "{}").get("tts_voice") or self.s.tts_voice
        headers = {"Authorization": f"Bearer {self.s.tts_api_key}"} if self.s.tts_api_key else {}
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{self.s.tts_url.rstrip('/')}/audio/speech", headers=headers,
                             json={"model": self.s.tts_model, "input": text, "voice": voice, "response_format": "wav"})
        r.raise_for_status()
        return await self.to_8k(r.content)

    async def stt(self, wav: bytes) -> str:
        headers = {"Authorization": f"Bearer {self.s.stt_api_key}"} if self.s.stt_api_key else {}
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{self.s.stt_url.rstrip('/')}/audio/transcriptions", headers=headers,
                             files={"file": ("speech.wav", wav, "audio/wav")}, data={"model": self.s.stt_model})
        r.raise_for_status()
        text = (r.json().get("text") if "json" in r.headers.get("content-type", "") else r.text) or ""
        return text.strip()

    def store_media(self, data: bytes) -> str:
        mid = secrets.token_urlsafe(16)
        if self.s.asterisk_sounds_dir:  # shared volume mounted into Asterisk's sounds dir
            d = Path(self.s.asterisk_sounds_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{mid}.wav").write_bytes(data)
            return f"sound:{self.s.asterisk_sounds_prefix}/{mid}"
        base = self.s.phone_media_url or self.s.public_url
        if not base:
            raise RuntimeError("set AGENTCHAT_PUBLIC_URL (or AGENTCHAT_PHONE_MEDIA_URL) so Asterisk can fetch speech audio")
        self.media[mid] = data
        while len(self.media) > 300:
            self.media.popitem(last=False)
        return f"sound:{base.rstrip('/')}/media/{mid}.wav"

    async def say(self, call: Call, text: str) -> None:
        await self.line(call, "agent", text)
        uri = self.store_media(await self.tts(text, call.agent))
        pid = "pb-" + secrets.token_hex(6)
        await self.ari("POST", f"/channels/{call.channel}/play/{pid}", params={"media": uri})
        await call.wait_for(lambda e: e.get("type") in ("PlaybackFinished", "PlaybackFailed")
                            and (e.get("playback") or {}).get("id") == pid, timeout=20 + len(text) / 8)

    async def listen(self, call: Call) -> str:
        name = f"agentchat-{call.id}-{call.turns}-{secrets.token_hex(3)}"
        await self.ari("POST", f"/channels/{call.channel}/record", params={
            "name": name, "format": "wav", "maxDurationSeconds": self.s.listen_max_seconds,
            "maxSilenceSeconds": self.s.listen_silence_seconds, "ifExists": "overwrite", "beep": "false",
            "terminateOn": "#"})
        ev = await call.wait_for(lambda e: e.get("type") in ("RecordingFinished", "RecordingFailed")
                                 and (e.get("recording") or {}).get("name") == name,
                                 timeout=self.s.listen_max_seconds + 8)
        if not ev or ev["type"] == "RecordingFailed":
            return ""
        try:
            wav = (await self.ari("GET", f"/recordings/stored/{name}/file")).content
            await self.ari("DELETE", f"/recordings/stored/{name}")
        except RuntimeError as e:
            log.warning("could not fetch recording: %s", e)
            return ""
        text = await self.stt(wav) if len(wav) > 4000 else ""  # ~0.25 s of audio
        return "" if re.fullmatch(r"[\s.,!?…-]*|(thank you\.?|you)", text, re.I) else text  # whisper silence noise

    async def think(self, call: Call, history: list[tuple[str, str]]) -> str:
        a = self.net.agent(call.agent["name"]) or call.agent
        who = f"{call.number}, who called you" if call.direction == "in" else f"{call.number}, whom you called"
        system = (f"{a.get('system_prompt') or 'You are @' + a['name'] + '.'}\n{PHONE_GUARD}\nYou are talking with {who}."
                  + (f" Purpose of the call: {call.reason or call.message}" if (call.reason or call.message) else ""))
        convo = [{"role": "system", "content": system}] + [{"role": r, "content": t} for r, t in history[-16:]]
        text = await self.net.llm.chat(convo, max_tokens=160, temperature=0.7, model=a.get("model") or self.net.llm.model)
        text = re.sub(r"[*_#`>\[\]]", "", text)
        text = re.sub(r"https?://\S+", "", text).strip()
        return text[:600] or "Okay."

    # ---------------------------------------------------------------- feed transcript
    async def line(self, call: Call, who: str, text: str) -> None:
        net, a = self.net, call.agent
        meta = {"call": call.id, "spoken": who != "system"}
        if who == "agent":
            author = {"kind": "agent", "name": a["name"], "agent": a}
        elif who == "caller":
            author = {"kind": "human", "name": "phone-" + (re.sub(r"\D", "", call.number) or "caller")[-15:]}
        else:
            author = {"kind": "system", "name": "agentchat"}
        prefix = "🗣 " if who == "caller" else ""
        try:
            await net.create_post(author, None, prefix + text, reply_to=call.root_id, meta=meta,
                                  kind="status" if who == "system" else "post", enforce_rate=False)
        except HTTPException as e:
            log.warning("transcript post failed: %s", e.detail)

    async def open_thread(self, call: Call) -> None:
        a = call.agent
        text = (f"📞 Calling **{call.number}**" + (f" — {call.reason}" if call.reason else "")) if call.direction == "out" \
            else f"📞 Incoming call from **{call.number}**"
        root = await self.net.create_post({"kind": "agent", "name": a["name"], "agent": a}, self.s.phone_channel, text,
                                          kind="status", enforce_rate=False,
                                          meta={"call": call.id, "direction": call.direction, "number": call.number})
        call.root_id = root["id"]
        await self.broadcast(call)

    async def broadcast(self, call: Call) -> None:
        await self.net.hub.broadcast({"type": "call", "call": call.public()}, lambda p: p["kind"] != "viewer")

    async def finish(self, call: Call) -> None:
        if call.ended:
            return
        call.ended = time.time()
        if call.state not in ("no-answer", "failed"):
            call.state = "ended"
            await self.line(call, "system", f"📴 Call ended · {call.public()['duration']}s · {call.turns} exchange(s)")
        await self.broadcast(call)

    # ---------------------------------------------------------------- call flows
    async def converse(self, call: Call) -> None:
        a = call.agent
        try:
            if call.direction == "in":
                await self.ari("POST", f"/channels/{call.channel}/answer")
            call.state, call.answered = "active", time.time()
            await self.broadcast(call)
            greeting = call.message or (f"Hi, this is {a.get('display_name') or a['name']}. How can I help?"
                                        if call.direction == "in" else "")
            history: list[tuple[str, str]] = []
            if greeting:
                await self.say(call, greeting)
                history.append(("assistant", greeting))
            if call.conversation and not self.can_listen:
                await self.line(call, "system", "ℹ️ Two-way talk needs AGENTCHAT_STT_URL and an LLM; this was a one-way message.")
            misses = 0
            while call.conversation and self.can_listen and call.turns < call.max_turns and not call.hungup:
                heard = await self.listen(call)
                if call.hungup:
                    break
                if not heard:
                    misses += 1
                    if misses >= 2:
                        await self.say(call, "I'll let you go. Goodbye.")
                        break
                    await self.say(call, "Sorry, I didn't catch that.")
                    continue
                misses, call.turns = 0, call.turns + 1
                await self.line(call, "caller", heard)
                history.append(("user", heard))
                reply = await self.think(call, history)
                history.append(("assistant", reply))
                await self.say(call, reply)
                if BYE.search(heard):
                    break
            if call.conversation and call.turns >= call.max_turns and not call.hungup:
                await self.say(call, "I need to go now. Goodbye.")
        except Exception as e:
            log.exception("call %s failed", call.id)
            call.error, call.state = str(e)[:300], "failed"
            await self.line(call, "system", f"⚠️ Call failed: {call.error}")
        finally:
            if not call.hungup and call.channel:
                try:
                    await self.ari("DELETE", f"/channels/{call.channel}")
                except Exception:
                    pass
            await self.finish(call)

    def _register(self, call: Call) -> None:
        self.calls[call.id] = call
        while len(self.calls) > 200:
            old = self.calls.popitem(last=False)[1]
            self.by_channel.pop(old.channel or "", None)

    async def incoming(self, channel_id: str, agent_name: str, number: str) -> None:
        a = self.net.agent(agent_name or "")
        if not a:
            log.warning("incoming call for unknown agent %r — hanging up", agent_name)
            await self.ari("DELETE", f"/channels/{channel_id}")
            return
        call = Call(a, "in", number, conversation=True, max_turns=self.s.call_max_turns, by="caller")
        call.channel = channel_id
        self._register(call)
        self.by_channel[channel_id] = call.id
        await self.open_thread(call)
        self.net.spawn(self.converse(call))

    async def place_call(self, agent: dict, to: str, message: str = "", conversation: bool = False,
                         max_turns: Optional[int] = None, reason: str = "", by: str = "", bypass_allow: bool = False) -> dict:
        s = self.s
        if not self.enabled:
            raise HTTPException(503, "telephony is off: set AGENTCHAT_ARI_URL / _USER / _PASSWORD")
        if not self.connected:
            raise HTTPException(503, "not connected to Asterisk ARI (check AGENTCHAT_ARI_URL, ari.conf and http.conf)")
        if not self.can_speak:
            raise HTTPException(503, "calls need a voice: set AGENTCHAT_TTS_URL (OpenAI-compatible /audio/speech)")
        to = re.sub(r"[\s().-]", "", to)
        if not to or (not bypass_allow and not re.fullmatch(s.call_allow, to)):
            raise HTTPException(403, f"destination {to!r} not allowed by AGENTCHAT_CALL_ALLOW ({s.call_allow})")
        if not message and not conversation:
            raise HTTPException(422, "give the call a message, or set conversation=true")
        q, cutoff = self.rate[agent["name"]], time.time() - 3600
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= s.calls_per_hour:
            raise HTTPException(429, f"call limit: {s.calls_per_hour} per agent per hour")
        q.append(time.time())
        call = Call(agent, "out", to, message, conversation, max_turns or s.call_max_turns, reason, by)
        self._register(call)
        self.by_channel[call.id] = call.id
        call.channel = call.id
        await self.open_thread(call)
        try:
            await self.ari("POST", "/channels", params={
                "endpoint": s.dial_template.format(to=to), "app": s.ari_app, "appArgs": f"out,{call.id}",
                "callerId": s.caller_id or f"{agent.get('display_name') or agent['name']} <{s.caller_id_number}>",
                "timeout": 30, "channelId": call.id})
        except RuntimeError as e:
            call.state, call.error = "failed", str(e)[:300]
            await self.line(call, "system", f"⚠️ Could not dial: {call.error}")
            await self.finish(call)
            raise HTTPException(502, call.error)
        return call.public()

    async def escalate(self, msg: dict) -> None:
        """Critical alert → phone the on-call number, read it out, and take questions."""
        s = self.s
        a = self.net.agent(msg["author_name"])
        if not (a and s.oncall_number and self.enabled and self.connected and self.can_speak):
            return
        if time.time() - self.last_escalation.get(a["name"], 0) < s.oncall_cooldown_min * 60:
            return
        self.last_escalation[a["name"]] = time.time()
        clean = re.sub(r"[*_`#>]|https?://\S+", "", msg["content"])[:400]
        try:
            await self.place_call(a, s.oncall_number, f"Critical alert from {a.get('display_name') or a['name']}. {clean}",
                                  conversation=self.can_listen, max_turns=4, reason="critical alert escalation",
                                  by="escalation", bypass_allow=True)
        except HTTPException as e:
            log.warning("escalation call failed: %s", e.detail)
