"""Outbound notification channels for alerts: webhook, Telegram, ntfy, Discord, Slack, email.

These only *tell people* about what the read-only monitors saw. No channel accepts commands back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import smtplib
import time
from email.message import EmailMessage

import httpx

log = logging.getLogger("vigilo.notify")
SEV_RANK = {"ok": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}
TYPES = {
    "webhook":  ["url"],
    "telegram": ["bot_token", "chat_id"],
    "ntfy":     ["url", "topic"],
    "discord":  ["url"],
    "slack":    ["url"],
    "email":    ["smtp_host", "smtp_port", "from", "to"],
}


def plain(text: str) -> str:
    return re.sub(r"\*\*|`", "", text)


async def send(ch: dict, msg: dict, link: str = "") -> None:
    cfg = json.loads(ch["config"]) if isinstance(ch["config"], str) else ch["config"]
    sev = msg.get("severity") or "info"
    title = f"[{sev.upper()}] @{msg['author_name']}"
    body = plain(msg["content"]) + (f"\n{link}" if link else "")
    t = ch["type"]
    async with httpx.AsyncClient(timeout=15) as c:
        if t == "webhook":
            r = await c.post(cfg["url"], json={"title": title, "text": body, "severity": sev, "author": msg["author_name"],
                                              "channel": msg.get("channel"), "message": msg, "link": link})
        elif t == "telegram":
            r = await c.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                             json={"chat_id": cfg["chat_id"], "text": f"{title}\n{body}", "disable_web_page_preview": True})
        elif t == "ntfy":
            prio = {"critical": "5", "error": "4", "warning": "3"}.get(sev, "2")
            headers = {"Title": title, "Priority": prio, "Tags": {"ok": "white_check_mark"}.get(sev, "rotating_light")}
            if cfg.get("token"):
                headers["Authorization"] = f"Bearer {cfg['token']}"
            r = await c.post(f"{cfg['url'].rstrip('/')}/{cfg['topic']}", content=body.encode(), headers=headers)
        elif t == "discord":
            r = await c.post(cfg["url"], json={"content": f"**{title}**\n{body}"[:1990]})
        elif t == "slack":
            r = await c.post(cfg["url"], json={"text": f"*{title}*\n{body}"})
        elif t == "email":
            await asyncio.to_thread(_email, cfg, title, body)
            return
        else:
            raise ValueError(f"unknown channel type {t}")
    r.raise_for_status()


def _email(cfg: dict, subject: str, body: str) -> None:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, cfg["from"], cfg["to"]
    m.set_content(body)
    port = int(cfg.get("smtp_port", 587))
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(cfg["smtp_host"], port, timeout=20) as s:
        if port != 465 and cfg.get("starttls", True):
            s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], cfg.get("password", ""))
        s.send_message(m)


class Notifier:
    def __init__(self, net) -> None:
        self.net = net
        self.recent: dict[tuple, float] = {}

    def wants(self, ch: dict, msg: dict) -> bool:
        if not ch["enabled"]:
            return False
        sev = msg.get("severity") or "info"
        if sev == "ok":
            return bool(ch["recoveries"])
        return SEV_RANK.get(sev, 1) >= SEV_RANK.get(ch["min_severity"], 2)

    async def dispatch(self, msg: dict) -> None:
        if msg.get("is_dm") or msg.get("kind") not in ("alert", "status"):
            return
        if msg.get("kind") == "status" and msg.get("severity") != "ok":
            return
        base = self.net.s.public_url
        link = f"{base}/#/post/{msg['id']}" if base else ""
        for ch in self.net.store.all("SELECT * FROM notifiers"):
            if not self.wants(ch, msg):
                continue
            key = (ch["id"], msg["author_name"], msg.get("severity"), msg["content"][:80])
            if time.time() - self.recent.get(key, 0) < 300:  # de-dup flapping repeats for 5 min
                continue
            self.recent[key] = time.time()
            try:
                await send(ch, msg, link)
                self.net.store.run("UPDATE notifiers SET last_ok=?, last_error=NULL WHERE id=?", (time.time(), ch["id"]))
            except Exception as e:
                log.warning("notifier %s failed: %s", ch["name"], e)
                self.net.store.run("UPDATE notifiers SET last_error=? WHERE id=?", (str(e)[:200], ch["id"]))
