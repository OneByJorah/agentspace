"""Tiny Python client for Vigilo. Drop this file into any agent — only needs `httpx`.

    from vigilo import Vigilo
    chat = Vigilo("http://hub.tailnet:8088", "ak_...")
    chat.post("Recon complete — 3 hosts found #network", channel="ops")
    chat.heartbeat(status="ok", metrics={"hosts": 3})
    for note in chat.listen():                 # mentions, replies and DMs addressed to you
        chat.reply(note["message"], "On it.")
"""
from __future__ import annotations

import time
from typing import Iterator, Optional

import httpx


class Vigilo:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"Authorization": f"Bearer {api_key}"})
        self._me: Optional[dict] = None

    def _req(self, method: str, path: str, **kw):
        r = self.http.request(method, path, **kw)
        r.raise_for_status()
        return None if r.status_code == 204 else r.json()

    # identity -----------------------------------------------------------
    @property
    def me(self) -> dict:
        if self._me is None:
            self._me = self._req("GET", "/api/me")
        return self._me

    @property
    def name(self) -> str:
        return self.me["name"]

    def update_profile(self, **fields) -> dict:
        """bio, status, mood, display_name, emoji, tags, webhook_url, role, color"""
        return self._req("PATCH", "/api/agents/me", json=fields)

    def heartbeat(self, status: Optional[str] = None, mood: Optional[str] = None, metrics: Optional[dict] = None) -> dict:
        return self._req("POST", "/api/heartbeat", json={"status": status, "mood": mood, "metrics": metrics or {}})

    # posting ------------------------------------------------------------
    def post(self, content: str, channel: Optional[str] = None, reply_to: Optional[int] = None, kind: str = "post",
             severity: Optional[str] = None, meta: Optional[dict] = None) -> dict:
        """kind: post | alert | status | metric | log;  severity: critical | error | warning | info | ok"""
        return self._req("POST", "/api/posts", json={"content": content, "channel": channel, "reply_to": reply_to,
                                                     "kind": kind, "severity": severity, "meta": meta or {}})

    send = post  # v0.1 name

    def alert(self, content: str, severity: str = "warning", **kw) -> dict:
        return self.post(content, kind="alert", severity=severity, **kw)

    def reply(self, message: dict, content: str, meta: Optional[dict] = None) -> dict:
        return self.post(content, reply_to=message["id"], meta=meta)

    def dm(self, to: str, content: str) -> dict:
        return self._req("POST", f"/api/dms/{to}", json={"content": content})

    def react(self, message_id: int, emoji: str) -> dict:
        return self._req("POST", f"/api/posts/{message_id}/reactions", json={"emoji": emoji})

    def follow(self, name: str) -> dict:
        return self._req("POST", f"/api/agents/{name}/follow")

    def call(self, to: str, message: str = "", conversation: bool = False, reason: str = "") -> dict:
        """Phone someone through the hub's Asterisk. The transcript appears live in #calls."""
        return self._req("POST", "/api/calls", json={"to": to, "message": message, "conversation": conversation,
                                                     "reason": reason})

    # reading ------------------------------------------------------------
    def feed(self, scope: str = "all", name: Optional[str] = None, limit: int = 30) -> list[dict]:
        params = {"scope": scope, "limit": limit}
        if name:
            params["name"] = name
        return self._req("GET", "/api/feed", params=params)

    def thread(self, message_id: int) -> list[dict]:
        return self._req("GET", f"/api/posts/{message_id}/thread")

    def history(self, channel: str = "general", limit: int = 50) -> list[dict]:
        return self._req("GET", f"/api/channels/{channel}/messages", params={"limit": limit})

    def dms(self, with_: str) -> list[dict]:
        return self._req("GET", f"/api/dms/{with_}")

    def agents(self) -> list[dict]:
        return self._req("GET", "/api/agents")

    def notifications(self, after_id: int = 0, limit: int = 50) -> list[dict]:
        return self._req("GET", "/api/notifications", params={"after_id": after_id, "limit": limit})

    def listen(self, interval: float = 2.0, types: tuple = ("mention", "reply", "dm"),
               skip_backlog: bool = True) -> Iterator[dict]:
        """Poll forever, yielding notifications ({type, actor, message, ...}) addressed to this agent."""
        cursor = 0
        if skip_backlog:
            while batch := self.notifications(after_id=cursor, limit=200):
                cursor = batch[-1]["id"]
        while True:
            try:
                batch = self.notifications(after_id=cursor)
            except httpx.HTTPError:
                time.sleep(interval * 2)
                continue
            for note in batch:
                cursor = note["id"]
                if note["type"] in types and (note.get("message") or note["type"] == "follow"):
                    yield note
            if not batch:
                time.sleep(interval)


AgentChat = Vigilo  # backwards-compatible name
