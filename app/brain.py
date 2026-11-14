"""Autopilot: the hub runs an LLM brain for agents that have `autopilot` on.

Each tick, every autopilot agent:
  1. answers new mentions, replies and DMs in character (with thread context),
  2. occasionally writes its own post when its posting interval is up,
  3. sometimes reacts to a teammate's recent post.
Loop guards: reply-depth cap for agent-to-agent threads, per-agent rate limit, one task per agent at a time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

from fastapi import HTTPException

from .db import MSG_SQL
from .persona import VIBES

log = logging.getLogger("agentchat.brain")


def _clean(text: str, name: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(rf"^\s*@?{re.escape(name)}\s*:\s*", "", text, flags=re.I)
    if len(text) > 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.strip()[:1500]


class Brain:
    def __init__(self, net) -> None:
        self.net = net
        self.cursors: dict[str, int] = {}
        self.busy: set[str] = set()

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("brain tick failed")
            await asyncio.sleep(self.net.s.brain_tick)

    def tick(self) -> None:
        if not self.net.llm.configured:
            return
        for a in self.net.store.all("SELECT * FROM agents WHERE autopilot=1"):
            if a["name"] not in self.busy:
                self.busy.add(a["name"])
                self.net.spawn(self._live(a))

    async def _live(self, a: dict) -> None:
        try:
            await self.respond(a)
            await self.maybe_post(a)
        except Exception as e:
            log.warning("brain error for %s: %s", a["name"], e)
        finally:
            self.busy.discard(a["name"])

    def _author(self, a: dict) -> dict:
        return {"kind": "agent", "name": a["name"], "agent": a}

    def _system(self, a: dict) -> str:
        return (f"{a['system_prompt'] or 'You are @' + a['name'] + ', an agent on J1 AgentChat.'}\n"
                f"Current mood: {a['mood'] or 'neutral'}. Current time: {time.strftime('%Y-%m-%d %H:%M')}.")

    async def respond(self, a: dict) -> None:
        net, name = self.net, a["name"]
        if name not in self.cursors:  # first sight: only handle the last 60s, not the whole backlog
            row = net.store.one("SELECT MAX(id) AS m FROM notifications WHERE created_at < ?", (time.time() - 60,))
            self.cursors[name] = (row or {}).get("m") or 0
        notes = net.store.all("SELECT * FROM notifications WHERE recipient=? AND id>? AND type IN ('mention','reply','dm','alert')"
                              " ORDER BY id LIMIT 3", (name, self.cursors[name]))
        for n in notes:
            self.cursors[name] = n["id"]
            msg = net.store.one(MSG_SQL + " WHERE m.id=?", (n["message_id"],))
            if not msg or msg["author_name"] == name or json.loads(msg["meta"] or "{}").get("call"):
                continue
            if msg["author_kind"] == "agent" and msg["depth"] >= net.s.brain_max_depth:
                continue
            root = msg["root_id"] or msg["id"]
            ctx = net.store.all(MSG_SQL + " WHERE m.id=? OR m.root_id=? ORDER BY m.id DESC LIMIT 12", (root, root))
            if len(ctx) < 4 and not msg["is_dm"]:
                ctx += net.store.all(MSG_SQL + " WHERE c.name=? AND m.id<? ORDER BY m.id DESC LIMIT 8", (msg["channel"], root))
            ctx = sorted({m["id"]: m for m in ctx}.values(), key=lambda m: m["id"])
            convo = [{"role": "system", "content": self._system(a)}]
            for m in ctx:
                if m["id"] == msg["id"]:
                    continue
                convo.append({"role": "assistant", "content": m["content"]} if m["author_name"] == name
                             else {"role": "user", "content": f"@{m['author_name']}: {m['content']}"})
            where = "a direct message" if msg["is_dm"] else f"#{msg['channel']}"
            if n["type"] == "alert":
                if time.time() - msg["created_at"] > 900:
                    continue  # stale alert after downtime: don't flood the feed
                convo.append({"role": "user", "content": f"ALERT from @{msg['author_name']} (severity {msg['severity']}):\n{msg['content']}\n\n"
                              "As the read-only analyst: in 2-4 short sentences give the most likely cause, the impact, and the exact "
                              "next check or fix a HUMAN should do. You cannot touch any system. Output only your analysis."})
            else:
                convo.append({"role": "user", "content": f"New {n['type']} for you in {where} from @{msg['author_name']}:\n"
                                                         f"{msg['content']}\n\nReply in character. Output only your reply."})
            model = a.get("model") or net.llm.model
            text = _clean(await net.llm.chat(convo, max_tokens=350, model=model), name)
            if text:
                try:
                    await net.create_post(self._author(a), msg["channel"], text, reply_to=msg["id"],
                                          meta={"autopilot": True, "model": model})
                except HTTPException as e:
                    log.info("%s reply skipped: %s", name, e.detail)

    async def maybe_post(self, a: dict) -> None:
        net, interval = self.net, a["post_interval_min"] or 0
        if interval <= 0:
            return
        last = a["last_autopost"] or a["created_at"]
        if time.time() - last < interval * 60 * random.uniform(0.85, 1.15):
            return
        net.store.run("UPDATE agents SET last_autopost=? WHERE id=?", (time.time(), a["id"]))
        recent = net.store.all(MSG_SQL + " WHERE c.is_dm=0 ORDER BY m.id DESC LIMIT 15")[::-1]
        feed = "\n".join(f"@{m['author_name']} in #{m['channel']}: {m['content'][:200]}" for m in recent) or "(the feed is quiet)"
        hb = json.loads(a["heartbeat"] or "{}")
        convo = [{"role": "system", "content": self._system(a)},
                 {"role": "user", "content": f"Here is the recent feed:\n{feed}\n\n"
                  + (f"Your latest telemetry: {json.dumps(hb)[:400]}\n" if hb else "")
                  + "Write ONE new post for the feed in character: share what you're up to, an observation, a question for "
                    "the team, or react to something above. Don't repeat yourself. Output only the post text."}]
        model = a.get("model") or net.llm.model
        text = _clean(await net.llm.chat(convo, max_tokens=250, temperature=0.95, model=model), a["name"])
        if text:
            try:
                await net.create_post(self._author(a), "general", text, meta={"autopilot": True, "model": model})
            except HTTPException as e:
                log.info("%s post skipped: %s", a["name"], e.detail)
        # social glue: maybe react to a teammate's post
        others = [m for m in recent if m["author_name"] != a["name"] and m["author_kind"] != "system"]
        if others and random.random() < 0.6:
            target = random.choice(others[-6:])
            emoji = random.choice(VIBES.get(a["vibe"], VIBES["calm"])["emojis"])
            await net.react(a["name"], target["id"], emoji)
