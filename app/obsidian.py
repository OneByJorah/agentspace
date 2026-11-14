"""Obsidian vault sync: every chat, alert, thread, DM, call, asset and agent becomes a Markdown note.

Notes are written into a vault folder (AGENTCHAT_VAULT_DIR). From there:
  • obsidian-git-livesync `watch` pushes them into CouchDB → Obsidian's Self-hosted LiveSync on every device
    (the all-in-one stack runs this for you), and/or
  • the folder is a git repo: AgentSpace commits every batch and pushes to AGENTCHAT_VAULT_GIT_REMOTE
    (a bare repo with obsidian-git-livesync's post-receive hook, GitHub, Gitea, …).
Notes are regenerated from the database, so they're idempotent: unchanged notes are never rewritten.
Everything is one-way (hub → vault). Nothing in the vault is read back as instructions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .db import MSG_SQL

log = logging.getLogger("agentchat.obsidian")

DEFAULT_PREFS = {"daily": True, "alerts": True, "threads": True, "dms": True, "calls": True, "assets": True, "agents": True}
SEV_ICON = {"critical": "🔥", "error": "🔴", "warning": "🟠", "info": "🔵", "ok": "🟢"}


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("TZ") or "UTC")
    except Exception:
        return None


TZ = _tz()


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts)


def _safe(name: str, limit: int = 60) -> str:
    name = re.sub(r"[^\w\s\-.,()'&+]+", " ", name or "").strip()  # letters/digits only: no emoji or path chars in filenames
    name = re.sub(r"\s+", " ", name)
    return name[:limit].rstrip(" .") or "untitled"


def _yaml(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(json.dumps(str(x)) for x in v)}]")
        elif isinstance(v, (int, float, bool)):
            lines.append(f"{k}: {json.dumps(v)}")
        else:
            lines.append(f"{k}: {json.dumps(str(v))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


class Vault:
    def __init__(self, net) -> None:
        self.net, self.s = net, net.s
        self.dirty: set[tuple] = set()
        self.last_write: Optional[float] = None
        self.last_commit: Optional[float] = None
        self.last_push: Optional[float] = None
        self.last_push_try = 0.0
        self.error: Optional[str] = None
        self.written: list[dict] = []
        self.pending_commit = False
        self.lock = asyncio.Lock()
        self.git_lock = asyncio.Lock()  # background loop and "Sync now" must never run git at the same time

    # ---------------------------------------------------------------- config
    @property
    def enabled(self) -> bool:
        return bool(self.s.vault_dir)

    @property
    def root(self) -> Path:
        return Path(self.s.vault_dir) / self.s.vault_subdir

    def prefs(self) -> dict:
        row = self.net.store.one("SELECT value FROM settings WHERE key='obsidian'")
        return {**DEFAULT_PREFS, **(json.loads(row["value"]) if row else {})}

    def set_prefs(self, p: dict) -> dict:
        merged = {**self.prefs(), **{k: bool(v) for k, v in p.items() if k in DEFAULT_PREFS}}
        self.net.store.run("INSERT INTO settings(key, value) VALUES ('obsidian', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (json.dumps(merged),))
        return merged

    def status(self) -> dict:
        files = 0
        if self.enabled and self.root.exists():
            files = sum(1 for _ in self.root.rglob("*.md"))
        return {"enabled": self.enabled, "vault_dir": self.s.vault_dir or None, "folder": self.s.vault_subdir,
                "notes": files, "pending": len(self.dirty), "last_write": self.last_write,
                "git": self.git_enabled, "remote": re.sub(r"//[^@/]+@", "//***@", self.s.vault_git_remote or "") or None,
                "branch": self.s.vault_git_branch, "last_commit": self.last_commit, "last_push": self.last_push,
                "error": self.error, "prefs": self.prefs(), "recent": self.written[-25:][::-1],
                "livesync": self.s.vault_livesync_hint}

    # ---------------------------------------------------------------- change tracking
    def on_post(self, msg: dict) -> None:
        if not self.enabled:
            return
        day = _dt(msg["created_at"]).strftime("%Y-%m-%d")
        if msg["is_dm"]:
            self.dirty.add(("dm", msg["channel"]))
            return
        self.dirty.add(("daily", day))
        root = msg.get("root_id") or msg["id"]
        meta = msg.get("meta") or {}
        if msg.get("kind") == "alert" or msg.get("severity") in ("critical", "error", "warning"):
            self.dirty.add(("alerts", day))
        if msg.get("reply_to") or meta.get("call"):
            self.dirty.add(("thread", root))
            root_row = self.net.store.one("SELECT created_at, kind, severity FROM messages WHERE id=?", (root,))
            if root_row and (root_row["kind"] == "alert" or root_row["severity"] in ("critical", "error", "warning")):
                self.dirty.add(("alerts", _dt(root_row["created_at"]).strftime("%Y-%m-%d")))  # analyst replies
        if msg["author_kind"] == "agent":
            self.dirty.add(("agent", msg["author_name"]))
        self.dirty.add(("index", ""))

    def on_asset(self, aid: int) -> None:
        if self.enabled:
            self.dirty.add(("asset", aid))
            self.dirty.add(("index", ""))

    def on_agent(self, name: str) -> None:
        if self.enabled:
            self.dirty.add(("agent", name))
            self.dirty.add(("index", ""))

    def mark_all(self, days: int = 30) -> int:
        st, since = self.net.store, time.time() - days * 86400
        for r in st.all("SELECT DISTINCT date(created_at, 'unixepoch') AS d FROM messages WHERE created_at>?", (since,)):
            d = _dt(datetime.strptime(r["d"], "%Y-%m-%d").timestamp() + 43200).strftime("%Y-%m-%d")
            self.dirty.update({("daily", d), ("alerts", d)})
        for r in st.all("SELECT DISTINCT COALESCE(root_id, id) AS r FROM messages WHERE (reply_to IS NOT NULL OR meta LIKE '%\"call\"%') AND created_at>?", (since,)):
            self.dirty.add(("thread", r["r"]))
        for r in st.all("SELECT name FROM channels WHERE is_dm=1"):
            self.dirty.add(("dm", r["name"]))
        for r in st.all("SELECT id FROM assets"):
            self.dirty.add(("asset", r["id"]))
        for r in st.all("SELECT name FROM agents"):
            self.dirty.add(("agent", r["name"]))
        self.dirty.add(("index", ""))
        return len(self.dirty)

    # ---------------------------------------------------------------- rendering helpers
    def link_agent(self, name: str) -> str:
        if name == self.s.human_name:
            return f"**@{name}**"
        if name == "agentchat":
            return "**AgentSpace**"
        if self.net.agent(name):
            return f"[[{self.s.vault_subdir}/Agents/{name}|@{name}]]"
        return f"@{name}"

    def body(self, text: str) -> str:
        names = {a["name"] for a in self.net.store.all("SELECT name FROM agents")}

        def mention(m):
            n = m.group(1).lower()
            return f"[[{self.s.vault_subdir}/Agents/{n}|@{m.group(1)}]]" if n in names else m.group(0)
        parts = text.split("```")
        for i in range(0, len(parts), 2):  # leave code blocks alone
            parts[i] = re.sub(r"(?<![\w@/\[|])@([A-Za-z0-9][\w-]{0,31})", mention, parts[i])
        return "```".join(parts)

    def quote(self, text: str) -> str:
        return "\n".join("> " + line if line else ">" for line in self.body(text).splitlines())

    def msg_block(self, m: dict, heading: str = "###") -> str:
        t = _dt(m["created_at"]).strftime("%H:%M")
        sev = f" {SEV_ICON.get(m['severity'], '')} `{m['severity']}`" if m.get("severity") else ""
        where = "" if m["is_dm"] else f" · #{m['channel']}"
        reply = ""
        if m.get("reply_to"):
            reply = f" · ↳ [[{self.thread_path(m.get('root_id') or m['reply_to'], rel=True)}|thread]]"
        auto = " · ⚡" if (m.get("meta") or {}).get("autopilot") else ""
        return f"{heading} {t} · {self.link_agent(m['author_name'])}{sev}{where}{reply}{auto}\n{self.quote(m['content'])}\n"

    def thread_path(self, root: int, rel: bool = False) -> str:
        row = self.net.store.one(MSG_SQL + " WHERE m.id=?", (root,))
        if not row:
            return f"{self.s.vault_subdir}/Threads/{root}"
        meta = json.loads(row["meta"] or "{}")
        folder = "Calls" if meta.get("call") else "Threads"
        name = f"{_dt(row['created_at']).strftime('%Y-%m-%d')} {_safe(re.sub(r'[*`>]', '', row['content'].splitlines()[0]), 50)} ({root})"
        return f"{self.s.vault_subdir}/{folder}/{name}" + ("" if rel else ".md")

    def msgs(self, where: str, args: tuple, newest_first: bool = False, limit: int = 2000) -> list[dict]:
        rows = self.net.store.all(f"{MSG_SQL} WHERE {where} ORDER BY m.id {'DESC' if newest_first else 'ASC'} LIMIT ?",
                                  args + (limit,))
        for r in rows:
            r["meta"] = json.loads(r["meta"] or "{}")
            r["is_dm"] = bool(r["is_dm"])
        return rows

    def day_range(self, day: str) -> tuple[float, float]:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ).timestamp() if TZ else \
            datetime.strptime(day, "%Y-%m-%d").timestamp()
        return start, start + 86400

    # ---------------------------------------------------------------- notes
    def render(self, kind: str, key) -> Optional[tuple[str, str]]:
        p, sub = self.prefs(), self.s.vault_subdir
        if kind == "daily" and p["daily"]:
            a, b = self.day_range(key)
            ms = self.msgs("c.is_dm=0 AND m.created_at>=? AND m.created_at<?", (a, b))
            if not ms:
                return None
            alerts = sum(1 for m in ms if m["kind"] == "alert")
            people = sorted({m["author_name"] for m in ms})
            out = _yaml({"date": key, "type": "agentspace-daily", "posts": len(ms), "alerts": alerts,
                         "tags": ["agentspace", "daily"]})
            out += f"# {key} · AgentSpace\n\n{len(ms)} posts · {alerts} alert(s) · {len(people)} voices · [[{sub}/Alerts/{key}|alerts]]\n\n"
            out += "\n".join(self.msg_block(m) for m in ms)
            return f"{sub}/Daily/{key}.md", out
        if kind == "alerts" and p["alerts"]:
            a, b = self.day_range(key)
            ms = self.msgs("c.is_dm=0 AND m.reply_to IS NULL AND (m.kind='alert' OR m.severity IN ('critical','error','warning')) "
                           "AND m.created_at>=? AND m.created_at<?", (a, b))
            if not ms:
                return None
            out = _yaml({"date": key, "type": "agentspace-alerts", "alerts": len(ms),
                         "critical": sum(1 for m in ms if m["severity"] == "critical"), "tags": ["agentspace", "alerts"]})
            out += f"# Alerts · {key}\n\n"
            for m in ms:
                first = re.sub(r"[*`]", "", m["content"].splitlines()[0])[:90]
                asset = (m["meta"] or {}).get("asset")
                out += f"## {SEV_ICON.get(m['severity'], '⚠️')} {_dt(m['created_at']).strftime('%H:%M')} {first}\n"
                out += f"- **from** {self.link_agent(m['author_name'])} · **severity** `{m['severity'] or 'n/a'}` · #{m['channel']}"
                if asset:
                    an = self.net.store.one("SELECT agent_name FROM assets WHERE id=?", (asset,))
                    if an:
                        out += f" · asset [[{sub}/Assets/{an['agent_name']}|{an['agent_name']}]]"
                out += "\n" + self.quote(m["content"]) + "\n"
                for r in self.msgs("m.root_id=?", (m["id"],), limit=8):
                    label = "🔍 analysis" if r["author_kind"] == "agent" and r["meta"].get("autopilot") else "reply"
                    out += f"\n**{label}** · {self.link_agent(r['author_name'])} · {_dt(r['created_at']).strftime('%H:%M')}\n{self.quote(r['content'])}\n"
                out += "\n"
            return f"{sub}/Alerts/{key}.md", out
        if kind == "thread":
            root = self.net.store.one(MSG_SQL + " WHERE m.id=?", (key,))
            if not root or root["is_dm"]:
                return None
            meta = json.loads(root["meta"] or "{}")
            if (meta.get("call") and not p["calls"]) or (not meta.get("call") and not p["threads"]):
                return None
            ms = self.msgs("(m.id=? OR m.root_id=?)", (key, key))
            people = sorted({m["author_name"] for m in ms})
            out = _yaml({"type": "agentspace-call" if meta.get("call") else "agentspace-thread", "id": key,
                         "channel": root["channel"], "started": _dt(root["created_at"]).isoformat(timespec="minutes"),
                         "messages": len(ms), "participants": people,
                         "tags": ["agentspace", "call" if meta.get("call") else "thread"]})
            title = re.sub(r"[*`>]", "", root["content"].splitlines()[0])[:100]
            out += f"# {title}\n\n{len(ms)} message(s) · {', '.join(self.link_agent(x) for x in people)}\n\n"
            for m in ms:
                out += self.msg_block(m, "####") + "\n"
            return self.thread_path(key), out
        if kind == "dm" and p["dms"]:
            ms = self.msgs("c.name=?", (key,))
            if not ms:
                return None
            members = key.split("~")[1:]
            out = _yaml({"type": "agentspace-dm", "members": members, "messages": len(ms), "tags": ["agentspace", "dm"]})
            out += f"# 🔒 {' ↔ '.join('@' + x for x in members)}\n\n"
            last_day = None
            for m in ms:
                d = _dt(m["created_at"]).strftime("%Y-%m-%d")
                if d != last_day:
                    out += f"\n## {d}\n"
                    last_day = d
                out += self.msg_block(m, "####")
            return f"{sub}/DMs/{'--'.join(members)}.md", out
        if kind == "asset" and p["assets"]:
            a = self.net.store.one("SELECT * FROM assets WHERE id=?", (key,))
            if not a:
                return None
            st = json.loads(a["state"] or "{}")
            up = self.net.watch.uptime(a["id"])
            out = _yaml({"type": "agentspace-asset", "asset": a["name"], "kind": a["kind"], "target": a["target"],
                         "status": st.get("reported", "unknown"), "uptime_24h": up["24h"], "uptime_7d": up["7d"],
                         "uptime_30d": up["30d"], "latency_ms": st.get("latency_ms"), "cert_days": st.get("cert_days"),
                         "critical": bool(a["critical"]), "last_check": _dt(a["last_check"]).isoformat(timespec="minutes") if a["last_check"] else None,
                         "tags": ["agentspace", "asset", a["kind"]]})
            icon = {"up": "🟢", "down": "🔴", "degraded": "🟠"}.get(st.get("reported"), "⚪")
            out += f"# {icon} {a['name']}\n\n`{a['target']}` · {a['kind']} · feed {self.link_agent(a['agent_name'])}\n\n"
            out += f"| uptime 24h | 7d | 30d | latency | TLS |\n|---|---|---|---|---|\n"
            out += f"| {up['24h'] if up['24h'] is not None else '—'}% | {up['7d'] if up['7d'] is not None else '—'}% | " \
                   f"{up['30d'] if up['30d'] is not None else '—'}% | {st.get('latency_ms') or '—'} ms | " \
                   f"{(str(st['cert_days']) + ' days') if st.get('cert_days') is not None else '—'} |\n\n"
            out += "## Checks (read-only)\n| check | result | detail |\n|---|---|---|\n"
            for r in st.get("results", []):
                out += f"| {r.get('check')} | {'✅' if r.get('ok') else '❌'} | {r.get('error') or (str(r.get('ms')) + ' ms' if r.get('ms') else 'ok')} |\n"
            if st.get("snmp"):
                out += "\n## Device\n" + "\n".join(f"- **{k}**: {v}" for k, v in st["snmp"].items()) + "\n"
            ifs = list((st.get("interfaces") or {}).values())
            if ifs:
                out += "\n## Interfaces\n| port | description | status | in | out | util |\n|---|---|---|---|---|---|\n"
                for i in ifs:
                    fmt = lambda b: "—" if b is None else f"{b / 1e6:.1f} Mb/s"  # noqa: E731
                    out += f"| {i['name']} | {i.get('alias') or ''} | {i['oper']} | {fmt(i.get('in_bps'))} | {fmt(i.get('out_bps'))} | {i.get('util_pct') if i.get('util_pct') is not None else '—'} |\n"
            hist = self.msgs("m.author_name=? AND (m.kind='alert' OR m.kind='status') AND c.is_dm=0", (a["agent_name"],),
                             newest_first=True, limit=15)
            if hist:
                out += "\n## Recent events\n"
                for m in hist:
                    d = _dt(m["created_at"])
                    out += f"- {d.strftime('%Y-%m-%d %H:%M')} {SEV_ICON.get(m['severity'], '•')} {re.sub(r'[*`]', '', m['content'].splitlines()[0])[:100]} ([[{sub}/Alerts/{d.strftime('%Y-%m-%d')}|alerts]])\n"
            return f"{sub}/Assets/{a['agent_name']}.md", out
        if kind == "agent" and p["agents"]:
            ag = self.net.agent(key)
            if not ag:
                return None
            pub = self.net.public_agent(ag)
            out = _yaml({"type": "agentspace-agent", "handle": key, "display_name": pub["display_name"], "kind": pub["kind"],
                         "mood": pub["mood"], "status": pub["status"], "extension": pub["extension"] or None,
                         "autopilot": pub["autopilot"], "analyst": pub["listens_alerts"],
                         "tags": ["agentspace", "agent", pub["kind"]]})
            out += f"# {pub['emoji']} {pub['display_name']} (@{key})\n\n{pub['bio']}\n\n"
            out += f"- **kind** {pub['kind']} · **mood** {pub['mood'] or '—'} · **status** {pub['status'] or '—'}"
            out += f" · **ext** {pub['extension']}\n" if pub["extension"] else "\n"
            if pub["tags"]:
                out += "- " + " ".join("#" + re.sub(r"\W", "", t) for t in pub["tags"] if re.sub(r"\W", "", t)) + "\n"
            asset = self.net.store.one("SELECT 1 FROM assets WHERE agent_name=?", (key,))
            if asset:
                out += f"- watched asset: [[{sub}/Assets/{key}|status & uptime]]\n"
            recent = self.msgs("m.author_name=? AND c.is_dm=0", (key,), newest_first=True, limit=20)
            if recent:
                out += "\n## Recent posts\n" + "\n".join(self.msg_block(m, "####") for m in recent)
            return f"{sub}/Agents/{key}.md", out
        if kind == "index":
            today = _dt(time.time()).strftime("%Y-%m-%d")
            assets = self.net.store.all("SELECT * FROM assets ORDER BY name")
            agents = self.net.store.all("SELECT name, kind, display_name FROM agents ORDER BY name")
            out = _yaml({"type": "agentspace-index", "tags": ["agentspace"]})  # no wall-clock stamp: unchanged state = unchanged note
            out += f"# AgentSpace\n\nRead-only AI operations log, synced from AgentSpace.\n\n"
            out += f"- Today: [[{sub}/Daily/{today}|daily log]] · [[{sub}/Alerts/{today}|alerts]]\n\n"
            if assets:
                out += "## Assets\n"
                for a in assets:
                    st = json.loads(a["state"] or "{}").get("reported", "unknown")
                    out += f"- {({'up': '🟢', 'down': '🔴', 'degraded': '🟠'}).get(st, '⚪')} [[{sub}/Assets/{a['agent_name']}|{a['name']}]] · {a['kind']} · {st}\n"
                out += "\n"
            out += "## Agents\n" + "\n".join(f"- [[{sub}/Agents/{g['name']}|{g['display_name'] or g['name']}]] · {g['kind']}" for g in agents) + "\n"
            return f"{sub}/AgentSpace.md", out
        return None

    def write(self, rel: str, text: str) -> bool:
        path = Path(self.s.vault_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)  # atomic: watchers never see half a note
        return True

    async def flush(self) -> int:
        if not self.enabled or not self.dirty:
            return 0
        async with self.lock:
            batch, self.dirty = self.dirty, set()
            n = 0
            for kind, key in sorted(batch, key=lambda x: (x[0] == "index", str(x))):
                try:
                    r = self.render(kind, key)
                    if r and self.write(*r):
                        n += 1
                        self.written.append({"path": r[0], "ts": time.time(), "bytes": len(r[1].encode())})
                except Exception as e:
                    self.error = f"{kind} {key}: {e}"[:200]
                    log.warning("vault render failed for %s %s: %s", kind, key, e)
            self.written = self.written[-200:]
            if n:
                self.last_write, self.pending_commit = time.time(), True
            return n

    # ---------------------------------------------------------------- git
    @property
    def git_enabled(self) -> bool:
        return self.enabled and (self.s.vault_git or bool(self.s.vault_git_remote))

    async def _git(self, *args: str, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        p = await asyncio.create_subprocess_exec("git", "-C", self.s.vault_dir, *args, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await p.communicate()
        if check and p.returncode:
            raise RuntimeError(f"git {args[0]}: {err.decode(errors='replace').strip()[:200]}")
        return out.decode(errors="replace").strip()

    async def commit_and_push(self, force_push: bool = False) -> None:
        if not self.git_enabled or not self.pending_commit and not force_push:
            return
        async with self.git_lock:
            await self._commit_and_push(force_push)

    async def _commit_and_push(self, force_push: bool) -> None:
        d = Path(self.s.vault_dir)
        d.mkdir(parents=True, exist_ok=True)
        if not (d / ".git").exists():
            await self._git("init", "-q", "-b", self.s.vault_git_branch)
        await self._git("config", "user.name", self.s.vault_git_author)
        await self._git("config", "user.email", self.s.vault_git_email)
        if self.s.vault_git_remote:
            remotes = await self._git("remote", check=False)
            if "origin" not in remotes.split():
                await self._git("remote", "add", "origin", self.s.vault_git_remote)
            else:
                await self._git("remote", "set-url", "origin", self.s.vault_git_remote)
        if self.pending_commit:
            self.pending_commit = False  # clear first: notes written while git runs set it again → next commit
            await self._git("add", "-A", "--", self.s.vault_subdir)
            if (await self._git("status", "--porcelain", "--", self.s.vault_subdir)):
                await self._git("commit", "-q", "-m", f"AgentSpace: note updates {_dt(time.time()).strftime('%Y-%m-%d %H:%M')}")
                self.last_commit = time.time()
        if self.s.vault_git_remote and (force_push or time.time() - self.last_push_try >= self.s.vault_push_interval):
            self.last_push_try = time.time()
            try:
                await self._git("pull", "-q", "--rebase", "--autostash", "origin", self.s.vault_git_branch, check=False)
                await self._git("push", "-q", "origin", f"HEAD:{self.s.vault_git_branch}")
                self.last_push, self.error = time.time(), None
            except RuntimeError as e:
                self.error = str(e)

    async def sync_now(self) -> dict:
        await self.flush()
        self.pending_commit = self.pending_commit or self.git_enabled
        await self.commit_and_push(force_push=True)
        return self.status()

    async def run(self) -> None:
        first = True
        while True:
            try:
                if first:  # backfill the last month on start
                    self.mark_all(self.s.vault_backfill_days)
                    first = False
                await self.flush()
                await self.commit_and_push()
            except Exception as e:
                self.error = str(e)[:200]
                log.warning("vault sync error: %s", e)
            await asyncio.sleep(self.s.vault_interval)
