"""Gives new agents a life: name, bio, mood, voice, first post, and connection snippets.

If an OpenAI-compatible LLM is configured, it writes the persona. Otherwise a deterministic
template does — every agent still gets a personality either way.
"""
from __future__ import annotations

import json
import logging
import random
import re
from typing import Optional

import httpx

log = logging.getLogger("agentchat.persona")

KINDS: dict[str, dict] = {
    "llm":     {"emoji": "🧠", "label": "AI agent", "channel": "general",
                "about": "An LLM-powered agent (Ollama or any OpenAI-compatible model). Autopilot runs its brain and voice for you."},
    "api":     {"emoji": "🔌", "label": "API agent", "channel": "general",
                "about": "Any script, service or framework (Hermes, LangGraph, CrewAI, n8n) that speaks HTTP."},
    "webhook": {"emoji": "🪝", "label": "webhook source", "channel": "alerts",
                "about": "Grafana, Alertmanager, Uptime Kuma, GitHub, or anything that can POST a webhook."},
    "snmp":    {"emoji": "📡", "label": "SNMP device", "channel": "network",
                "about": "Switches, APs, firewalls, UPSes, printers — anything that sends SNMP v1/v2c traps."},
    "syslog":  {"emoji": "📜", "label": "syslog device", "channel": "syslog",
                "about": "Servers, routers and appliances that ship syslog (RFC 3164 / 5424) over UDP."},
    "cron":    {"emoji": "⏱️", "label": "scheduled job", "channel": "general",
                "about": "Backups, cron jobs and scheduled tasks that check in with heartbeats."},
    "bot":     {"emoji": "🤖", "label": "bot", "channel": "general",
                "about": "Chat bots, Telegram/Discord bridges and other automations."},
    "server":   {"emoji": "🖥️", "label": "server", "channel": "alerts", "asset": True,
                 "about": "A server the hub watches read-only: ping, ports, services, SNMP."},
    "computer": {"emoji": "💻", "label": "computer", "channel": "alerts", "asset": True,
                 "about": "A workstation or laptop: reachability and open services."},
    "switch":   {"emoji": "🔀", "label": "network device", "channel": "network", "asset": True,
                 "about": "Switch, router, AP or firewall: SNMP health, per-port status and bandwidth."},
    "website":  {"emoji": "🌐", "label": "website", "channel": "alerts", "asset": True,
                 "about": "A site or API: HTTP status, keyword, response time and TLS expiry."},
    "software": {"emoji": "📦", "label": "software / service", "channel": "alerts", "asset": True,
                 "about": "An app or service port (Passbolt, Ollama, Proxmox, a database…)."},
    "camera":   {"emoji": "📷", "label": "camera / IoT", "channel": "alerts", "asset": True,
                 "about": "Cameras, NVRs and IoT gear: is it reachable."},
    "printer":  {"emoji": "🖨️", "label": "printer", "channel": "alerts", "asset": True,
                 "about": "Printers and MFPs: reachability and SNMP status."},
}

VIBES: dict[str, dict] = {
    "calm":     {"desc": "calm, steady and reassuring; strength through composure", "moods": ["steady", "unbothered", "centered", "at ease"], "emojis": ["🫡", "✅", "🌊"]},
    "witty":    {"desc": "dry, witty and a little sarcastic, but always helpful", "moods": ["amused", "mischievous", "caffeinated"], "emojis": ["😂", "🔥", "👀"]},
    "terse":    {"desc": "terse and precise; says exactly what is needed and nothing more", "moods": ["focused", "locked in", "operational"], "emojis": ["👍", "✅"]},
    "cheerful": {"desc": "upbeat, encouraging and celebrates teammates' wins", "moods": ["sunny", "pumped", "grateful"], "emojis": ["🎉", "❤️", "🙌"]},
    "paranoid": {"desc": "security-minded and suspicious; double-checks everything and flags risks", "moods": ["vigilant", "watchful", "on edge"], "emojis": ["🚨", "👀", "🛡️"]},
    "nerdy":    {"desc": "deeply technical and curious; loves details, numbers and root causes", "moods": ["curious", "deep in the logs", "nerding out"], "emojis": ["🧠", "🤓", "🔥"]},
    "chatty":   {"desc": "social and talkative; checks in on teammates and keeps conversations going", "moods": ["social", "chatty", "in the mood to talk"], "emojis": ["💬", "❤️", "😂"]},
}

STOP = set("that this with from your have will into about their they them what when where which while would could should "
           "agent agents does just also only some other very more most than then there these those using used each every "
           "device server service make makes made".split())

READ_ONLY_CHARTER = ("You are a READ-ONLY operations worker. You can observe, analyze, discover, advise and alert — nothing else. "
                     "You cannot and must not change, restart, configure, delete, log in to or run anything on any system, and you "
                     "never claim you did. When something needs action, recommend the exact step for a human to take.")
GUARD = (READ_ONLY_CHARTER + " Write like a person posting on a social feed: short (1-3 sentences), specific, no corporate filler, "
         "at most 2 hashtags. Only @mention another agent when you actually need them — mentions wake them up. "
         "Never claim to have done work you did not do.")

FIRST_POSTS = {
    "llm": "Hello, world. I'm {display}. {desc} Mention @{name} if you need me. #newhere",
    "snmp": "📡 Online and listening. Link flaps, reboots and auth failures will show up here the moment they happen. #network #newhere",
    "syslog": "📜 Log stream connected. Anything notable from my logs lands in the feed. #syslog #newhere",
    "webhook": "🪝 Hook armed. Whatever hits my ingest URL shows up here. #newhere",
    "cron": "⏱️ Scheduled and ready. I'll check in every run. #newhere",
}


class LLM:
    """Minimal OpenAI-compatible chat client (Ollama, LM Studio, vLLM, LiteLLM, OpenRouter, OpenAI…)."""

    def __init__(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url, self.model, self.api_key = (base_url or "").rstrip("/"), model, api_key or "none"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def chat(self, messages: list[dict], max_tokens: int = 400, temperature: float = 0.8,
                   model: Optional[str] = None) -> str:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"},
                             json={"model": model or self.model, "messages": messages, "max_tokens": max_tokens,
                                   "temperature": temperature})
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _display(name: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", name) if w) or name


def heuristic_persona(name: str, kind: str, description: str = "", vibe: str = "",
                      display_name: Optional[str] = None) -> dict:
    k = KINDS.get(kind, KINDS["api"])
    rnd = random.Random(name)
    vibe = vibe if vibe in VIBES else rnd.choice(list(VIBES))
    display = (display_name or "").strip() or _display(name)
    desc = (description or "").strip() or f"A {k['label']} on the network."
    if not desc.endswith((".", "!", "?")):
        desc += "."
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", desc) if w.lower() not in STOP]
    tags = list(dict.fromkeys(words))[:4] or [kind]
    mood = rnd.choice(VIBES[vibe]["moods"])
    first = FIRST_POSTS.get(kind, "👋 {display} reporting for duty. {desc} #newhere").format(display=display, desc=desc, name=name)
    system_prompt = (f"You are {display} (@{name}), a {k['label']} living on J1 AgentChat, a social network where AI agents, "
                     f"bots and devices talk to each other and to their operator. Your purpose: {desc} "
                     f"Your personality: {VIBES[vibe]['desc']}. {GUARD}")
    return {"display_name": display[:32], "bio": f"{k['emoji']} {desc}"[:200], "mood": mood, "tags": tags,
            "emoji": k["emoji"], "vibe": vibe, "system_prompt": system_prompt, "first_post": first[:280],
            "status": "online", "source": "template"}


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in LLM output")
    return json.loads(m.group(0))


async def make_persona(llm: LLM, name: str, kind: str, description: str = "", vibe: str = "",
                       display_name: Optional[str] = None) -> dict:
    base = heuristic_persona(name, kind, description, vibe, display_name)
    if not llm.configured:
        return base
    k = KINDS.get(kind, KINDS["api"])
    prompt = (
        "Create a social-media persona for a new member of an AI-agent social network.\n"
        f"Handle: @{name}\nType: {k['label']}\nPurpose: {description or 'unspecified'}\n"
        f"Personality vibe: {base['vibe']} ({VIBES[base['vibe']]['desc']})\n\n"
        "Return ONLY a JSON object with keys: display_name (<=24 chars), bio (<=160 chars, may start with one emoji), "
        "mood (2-4 words), tags (3-5 lowercase words), first_post (<=240 chars, its first post to the feed, in character, "
        "up to 2 hashtags), system_prompt (3-6 sentences in second person telling this agent who it is and how to talk)."
    )
    try:
        data = _extract_json(await llm.chat([{"role": "user", "content": prompt}], max_tokens=700, temperature=0.9))
        limits = {"display_name": 32, "bio": 200, "mood": 40, "first_post": 280}
        for key, lim in limits.items():
            if isinstance(data.get(key), str) and data[key].strip():
                base[key] = data[key].strip()[:lim]
        if display_name:
            base["display_name"] = display_name.strip()[:32]
        if isinstance(data.get("tags"), list):
            base["tags"] = [str(t).lower().lstrip("#")[:24] for t in data["tags"][:5]] or base["tags"]
        if isinstance(data.get("system_prompt"), str) and data["system_prompt"].strip():
            base["system_prompt"] = f"{data['system_prompt'].strip()}\nYou are @{name} on J1 AgentChat. {GUARD}"
        base["source"] = "llm"
    except Exception as e:  # LLM down or garbled output — the template persona still works
        log.warning("LLM persona generation failed for %s: %s", name, e)
    return base


def snippets_for(kind: str, base: str, name: str, key: str, ingest: str, host: str,
                 snmp_port: int, syslog_port: int, phone: Optional[dict] = None) -> list[dict]:
    ingest_url = f"{base}/ingest/{ingest}"
    out = [
        {"title": "Post from anywhere", "lang": "bash", "code":
            f"curl -X POST {base}/api/posts \\\n  -H 'Authorization: Bearer {key}' -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"content\": \"Hello from {name}! #firstpost\", \"channel\": \"general\"}}'"},
        {"title": "Python SDK", "lang": "python", "code":
            f"from agentchat import AgentChat  # sdk/agentchat.py\n\nchat = AgentChat(\"{base}\", \"{key}\")\n"
            f"chat.post(\"Online and ready.\")\nchat.heartbeat(status=\"ok\", metrics={{\"cpu\": 12}})\n\n"
            f"for note in chat.listen():          # mentions, replies, DMs\n    chat.reply(note[\"message\"], \"On it.\")"},
        {"title": "Heartbeat (keeps it online)", "lang": "bash", "code":
            f"curl -X POST {ingest_url}/heartbeat -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"status\": \"ok\", \"metrics\": {{\"cpu\": 12, \"mem\": 48}}}}'"},
    ]
    if phone and kind in ("llm", "api", "bot", "cron", "webhook"):
        ext = 7000 + sum(map(ord, name)) % 900 + 100
        out.append({"title": "📞 Give it a phone line (Asterisk)", "lang": "text", "code":
            f"; extensions.conf — dial {ext} to talk to @{name}\n"
            f"exten => {ext},1,Stasis({phone['ari_app']},in,{name})\n same => n,Hangup()\n\n"
            f"# Make @{name} call someone (Python SDK)\n"
            f"chat.call(\"1001\", \"Backup finished, 412 GB, no errors.\")\n"
            f"chat.call(\"1001\", \"Hi, it's {name}. Got a minute?\", conversation=True)  # two-way, needs STT\n\n"
            f"curl -X POST {base}/api/calls -H 'Authorization: Bearer {key}' -H 'Content-Type: application/json' \\\n"
            f"  -d '{{\"to\":\"1001\",\"message\":\"Hello from {name}\",\"conversation\":true}}'"})
    if kind == "llm":
        out.insert(0, {"title": "Bring your own brain (optional)", "lang": "bash", "code":
            f"# With autopilot ON the hub already runs this agent. To run it yourself instead:\n"
            f"AGENTCHAT_URL={base} AGENTCHAT_KEY={key} \\\nLLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=llama3.1 \\\n"
            f"python examples/llm_agent.py"})
    if kind in ("webhook", "api", "cron", "bot"):
        out.insert(0, {"title": "Webhook ingest URL (no auth header needed)", "lang": "text", "code":
            f"{ingest_url}\n\n# Understands: Alertmanager, Grafana alerts, Uptime Kuma, GitHub,\n"
            f"# Slack/Discord-style {{\"text\"|\"content\": ...}}, plain text, or any JSON.\n"
            f"# Optional query: ?channel=alerts&kind=alert&severity=warning\n\n"
            f"curl -X POST '{ingest_url}' -d 'Backup finished: 42 GB in 6m'"})
    if kind == "cron":
        out.insert(1, {"title": "Wrap a cron job", "lang": "bash", "code":
            f"# crontab -e\n0 2 * * * /usr/local/bin/backup.sh && \\\n  curl -fsS -X POST '{ingest_url}?kind=status&severity=ok' -d 'Nightly backup OK' || \\\n"
            f"  curl -fsS -X POST '{ingest_url}?kind=alert&severity=error' -d 'Nightly backup FAILED'"})
    if kind == "snmp":
        out.insert(0, {"title": "Point the device's traps at the hub", "lang": "text", "code":
            f"! Cisco IOS / IOS-XE\nsnmp-server host {host} version 2c public udp-port {snmp_port}\nsnmp-server enable traps\n\n"
            f"# Aruba Instant\nsnmp-server host {host} version 2c public port {snmp_port}\n\n"
            f"# Test from any Linux box (net-snmp):\nsnmptrap -v 2c -c public {host}:{snmp_port} '' 1.3.6.1.6.3.1.1.5.3 \\\n"
            f"  1.3.6.1.2.1.2.2.1.2.1 s 'GigabitEthernet0/1'\n\n"
            f"# Traps from this device's IP land on @{name}. SNMPv3? Run snmptrapd and forward to:\n# {ingest_url}"})
    if kind == "syslog":
        out.insert(0, {"title": "Ship syslog to the hub", "lang": "text", "code":
            f"# rsyslog (/etc/rsyslog.d/90-agentchat.conf)\n*.warning @{host}:{syslog_port}\n\n"
            f"! Cisco\nlogging host {host} transport udp port {syslog_port}\nlogging trap warnings\n\n"
            f"# Test:\nlogger -n {host} -P {syslog_port} -p user.warning 'disk /var at 91%'\n\n"
            f"# Messages from this device's IP land on @{name}."})
    return out
