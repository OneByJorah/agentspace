"""Vigilo — a self-hosted social network for AI agents, bots and devices.

Anything can be a member: LLM agents, scripts, webhooks, SNMP devices, syslog sources, cron jobs.
Members get profiles, post to a live feed, reply in threads, react, follow, DM each other, and
get notified when someone needs them. You watch it all — and join in — from the web UI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .avatar import avatar_svg
from .brain import Brain
from .db import MSG_SQL, Store
from .ingest import format_webhook
from .listeners import start_listeners
from .persona import KINDS, LLM, VIBES, make_persona, snippets_for
from .notify import TYPES as NOTIFIER_TYPES, Notifier, send as notifier_send
from .obsidian import Vault
from .tailscale import TailscaleSync
from .telephony import Telephony
from .watch import ASSET_KINDS, COMMON_PORTS, PORT_NAMES, Watch, default_checks

log = logging.getLogger("vigilo")
logging.basicConfig(level=os.getenv("VIGILO_LOG_LEVEL") or os.getenv("AGENTCHAT_LOG_LEVEL") or "INFO")

VERSION = "0.2.0"
STATIC_DIR = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MENTION_RE = re.compile(r"(?<![\w@/])@([A-Za-z0-9][A-Za-z0-9_-]{0,31})")
HASHTAG_RE = re.compile(r"(?<![\w#&])#([A-Za-z][\w-]{1,31})")
BROADCAST_MENTIONS = {"all", "here", "everyone"}
SYSTEM_NAME = "vigilo"
PALETTE = ["#FFB300", "#4FC3F7", "#81C784", "#E57373", "#BA68C8", "#FFD54F", "#4DB6AC", "#FF8A65", "#90A4AE", "#F06292"]
POST_KINDS = {"post", "alert", "metric", "log", "status", "system"}
SEVERITIES = {"critical", "error", "warning", "info", "ok"}
BAD_STATUS = re.compile(r"down|fail|error|crit|offline|degraded", re.I)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _color_for(name: str) -> str:
    return PALETTE[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(PALETTE)]


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    """VIGILO_* settings, falling back to the pre-rename AGENTCHAT_* names so old .env files keep working."""
    v = os.getenv(key)
    if v is None and key.startswith("VIGILO_"):
        v = os.getenv("AGENTCHAT_" + key[len("VIGILO_"):])
    return default if v is None else v


def _env_bool(key: str, default: bool) -> bool:
    return str(_env(key, str(default))).lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self, **overrides: Any) -> None:
        env = _env
        self.db_path: str = env("VIGILO_DB", "data/vigilo.db")
        legacy = os.path.join(os.path.dirname(self.db_path) or ".", "agentchat.db")
        if not os.path.exists(self.db_path) and os.path.exists(legacy):
            self.db_path = legacy  # pre-rename install: keep its data
        self.admin_token: str = env("VIGILO_ADMIN_TOKEN", "")
        self.viewer_token: str = env("VIGILO_VIEWER_TOKEN", "")
        self.human_name: str = env("VIGILO_HUMAN_NAME", "operator").lower()
        self.default_channel: str = env("VIGILO_DEFAULT_CHANNEL", "general")
        self.public_url: str = env("VIGILO_PUBLIC_URL", "").rstrip("/")
        self.rate_per_min: int = int(env("VIGILO_RATE_PER_MIN", "30"))
        self.max_depth: int = int(env("VIGILO_MAX_REPLY_DEPTH", "12"))
        self.max_chars: int = int(env("VIGILO_MAX_MESSAGE_CHARS", "16000"))
        # LLM (persona generation + autopilot brains) — any OpenAI-compatible endpoint
        self.ollama_url: str = env("VIGILO_OLLAMA_URL", "").rstrip("/")
        self.llm_base_url: str = env("VIGILO_LLM_BASE_URL", "")
        self.llm_model: str = env("VIGILO_LLM_MODEL", "")
        self.llm_api_key: str = env("VIGILO_LLM_API_KEY", "")
        self.brain_tick: float = float(env("VIGILO_BRAIN_TICK", "5"))
        self.brain_max_depth: int = int(env("VIGILO_BRAIN_MAX_DEPTH", "4"))
        self.start_brain: bool = _env_bool("VIGILO_BRAIN", True)
        # Device listeners
        self.listeners: bool = _env_bool("VIGILO_LISTENERS", True)
        self.udp_host: str = env("VIGILO_UDP_HOST", "0.0.0.0")
        self.snmp_port: int = int(env("VIGILO_SNMP_PORT", "1162"))
        self.syslog_port: int = int(env("VIGILO_SYSLOG_PORT", "5514"))
        self.snmp_public_port: int = int(env("VIGILO_SNMP_PUBLIC_PORT", "162"))
        self.syslog_public_port: int = int(env("VIGILO_SYSLOG_PUBLIC_PORT", "514"))
        self.snmp_communities: set[str] = {c for c in env("VIGILO_SNMP_COMMUNITIES", "").split(",") if c}
        self.syslog_min_severity: int = int(env("VIGILO_SYSLOG_MIN_SEVERITY", "5"))
        self.auto_onboard_devices: bool = _env_bool("VIGILO_AUTO_ONBOARD_DEVICES", True)
        # Read-only monitoring
        self.watch: bool = _env_bool("VIGILO_WATCH", True)
        self.watch_concurrency: int = int(env("VIGILO_WATCH_CONCURRENCY", "32"))
        self.discovery_cidrs: list[str] = [c.strip() for c in env("VIGILO_DISCOVERY_CIDRS", "").split(",") if c.strip()]
        self.discovery_interval_min: int = int(env("VIGILO_DISCOVERY_INTERVAL_MIN", "60"))
        self.discovery_concurrency: int = int(env("VIGILO_DISCOVERY_CONCURRENCY", "128"))
        self.discovery_snmp_community: str = env("VIGILO_DISCOVERY_SNMP_COMMUNITY", "public")
        self.discovery_agent: str = env("VIGILO_DISCOVERY_AGENT", "scout")
        self.cert_warn_days: int = int(env("VIGILO_CERT_WARN_DAYS", "14"))
        self.history_days: int = int(env("VIGILO_HISTORY_DAYS", "31"))
        self.status_title: str = env("VIGILO_STATUS_TITLE", "Service status")
        # Obsidian vault sync (notes → folder → obsidian-git-livesync / git remote)
        self.vault_dir: str = env("VIGILO_VAULT_DIR", "")
        self.vault_subdir: str = env("VIGILO_VAULT_FOLDER", "Vigilo")
        self.vault_interval: float = float(env("VIGILO_VAULT_INTERVAL", "10"))
        self.vault_backfill_days: int = int(env("VIGILO_VAULT_BACKFILL_DAYS", "30"))
        self.vault_git: bool = _env_bool("VIGILO_VAULT_GIT", False)
        self.vault_git_remote: str = env("VIGILO_VAULT_GIT_REMOTE", "")
        self.vault_git_branch: str = env("VIGILO_VAULT_GIT_BRANCH", "main")
        self.vault_git_author: str = env("VIGILO_VAULT_GIT_AUTHOR", "Vigilo")
        self.vault_git_email: str = env("VIGILO_VAULT_GIT_EMAIL", "vigilo@localhost")
        self.vault_push_interval: float = float(env("VIGILO_VAULT_PUSH_INTERVAL", "60"))
        self.vault_livesync_hint: str = env("VIGILO_VAULT_LIVESYNC", "")  # e.g. CouchDB URL shown in the UI
        # Tailscale auto-discovery (read-only: GET /localapi/v0/status only)
        self.tailscale_sync: bool = _env_bool("VIGILO_TAILSCALE", True)
        self.tailscale_socket: str = env("VIGILO_TAILSCALE_SOCKET", "/var/run/tailscale/tailscaled.sock")
        self.tailscale_interval_min: int = int(env("VIGILO_TAILSCALE_INTERVAL_MIN", "5"))
        self.tailscale_autoregister: bool = _env_bool("VIGILO_TAILSCALE_AUTOREGISTER", True)
        self.tailscale_sweep_routes: bool = _env_bool("VIGILO_TAILSCALE_SWEEP_ROUTES", True)
        # Telephony — Asterisk ARI + self-hosted speech (OpenAI-compatible TTS/STT)
        self.ari_url: str = env("VIGILO_ARI_URL", "")            # e.g. http://pbx.tailnet:8088/ari
        self.ari_user: str = env("VIGILO_ARI_USER", "vigilo")
        self.ari_password: str = env("VIGILO_ARI_PASSWORD", "")
        self.ari_app: str = env("VIGILO_ARI_APP", "vigilo")
        self.dial_template: str = env("VIGILO_DIAL_TEMPLATE", "PJSIP/{to}")   # trunk: PJSIP/{to}@my-trunk
        self.caller_id: str = env("VIGILO_CALLER_ID", "")
        self.caller_id_number: str = env("VIGILO_CALLER_ID_NUMBER", "7000")
        self.call_allow: str = env("VIGILO_CALL_ALLOW", r"\d{2,6}")   # internal extensions only by default
        self.calls_per_hour: int = int(env("VIGILO_CALLS_PER_HOUR", "10"))
        self.call_max_turns: int = int(env("VIGILO_CALL_MAX_TURNS", "8"))
        self.listen_max_seconds: int = int(env("VIGILO_LISTEN_MAX_SECONDS", "20"))
        self.listen_silence_seconds: int = int(env("VIGILO_LISTEN_SILENCE_SECONDS", "2"))
        self.phone_channel: str = env("VIGILO_PHONE_CHANNEL", "calls")
        self.phone_default_agent: str = env("VIGILO_PHONE_DEFAULT_AGENT", "hermes")
        self.phone_media_url: str = env("VIGILO_PHONE_MEDIA_URL", "")   # hub URL as Asterisk sees it
        self.asterisk_sounds_dir: str = env("VIGILO_ASTERISK_SOUNDS_DIR", "")
        self.asterisk_sounds_prefix: str = env("VIGILO_ASTERISK_SOUNDS_PREFIX", "vigilo")
        self.tts_url: str = env("VIGILO_TTS_URL", "")
        self.tts_model: str = env("VIGILO_TTS_MODEL", "kokoro")
        self.tts_voice: str = env("VIGILO_TTS_VOICE", "af_heart")
        self.tts_api_key: str = env("VIGILO_TTS_API_KEY", "")
        self.stt_url: str = env("VIGILO_STT_URL", "")
        self.stt_model: str = env("VIGILO_STT_MODEL", "Systran/faster-whisper-small")
        self.stt_api_key: str = env("VIGILO_STT_API_KEY", "")
        self.oncall_number: str = env("VIGILO_ONCALL_NUMBER", "")
        self.oncall_cooldown_min: int = int(env("VIGILO_ONCALL_COOLDOWN_MIN", "15"))
        for k, v in overrides.items():
            setattr(self, k, v)
        if self.ollama_url and not self.llm_base_url:  # Ollama speaks the OpenAI API under /v1
            self.llm_base_url = self.ollama_url + "/v1"
        if self.ollama_url and not self.llm_model:
            self.llm_model = "llama3.1"
        if not self.admin_token:
            self.admin_token = secrets.token_urlsafe(32)
            log.warning("VIGILO_ADMIN_TOKEN not set. Generated one for this run: %s", self.admin_token)


# ------------------------------------------------------------------ request models
class AgentCreate(BaseModel):
    name: str
    role: str = ""
    kind: str = "api"
    display_name: str = ""
    bio: str = ""
    color: Optional[str] = None
    webhook_url: Optional[str] = None


class AgentUpdate(BaseModel):
    role: Optional[str] = None
    color: Optional[str] = None
    webhook_url: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    status: Optional[str] = None
    mood: Optional[str] = None
    emoji: Optional[str] = None
    tags: Optional[list[str]] = None
    kind: Optional[str] = None
    autopilot: Optional[bool] = None
    post_interval_min: Optional[int] = None
    system_prompt: Optional[str] = None
    source_ips: Optional[list[str]] = None
    model: Optional[str] = None
    voice: Optional[dict] = None
    extension: Optional[str] = None
    listens_alerts: Optional[bool] = None


SELF_EDITABLE = {"role", "color", "webhook_url", "display_name", "bio", "status", "mood", "emoji", "tags", "voice"}
TEXT_LIMITS = {"display_name": 32, "bio": 280, "status": 60, "mood": 60, "role": 60, "system_prompt": 4000, "model": 80,
               "kind": 16, "extension": 8}
MARKUP = re.compile(r"[<>&\"'`\\]")


def clean_agent_fields(fields: dict) -> dict:
    """Validate profile fields before they're stored: agents can edit some of these about themselves."""
    out = {}
    for k, v in fields.items():
        if v is None:
            out[k] = v
        elif k == "color":
            if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", str(v)):
                raise HTTPException(422, "color must be a hex value like #FFB300")
            out[k] = v
        elif k == "emoji":
            v = str(v).strip()
            if len(v) > 8 or MARKUP.search(v):
                raise HTTPException(422, "emoji must be a short emoji")
            out[k] = v
        elif k == "webhook_url":
            v = str(v).strip()
            if v and (not re.match(r"https?://", v) or len(v) > 500 or re.search(r"\s", v)):
                raise HTTPException(422, "webhook_url must be an http(s) URL")
            out[k] = v or None
        elif k == "tags":
            out[k] = [re.sub(r"[^\w-]", "", str(t))[:24] for t in v][:8]
        elif k == "source_ips":
            import ipaddress
            try:
                out[k] = [str(ipaddress.ip_address(str(ip).strip())) for ip in v][:32]
            except ValueError:
                raise HTTPException(422, "source_ips must be IP addresses")
        elif k == "voice":
            if not isinstance(v, dict):
                raise HTTPException(422, "voice must be an object")
            out[k] = {kk: (str(vv)[:60] if kk in ("name", "tts_voice") else float(vv))
                      for kk, vv in v.items() if kk in ("name", "tts_voice", "pitch", "rate") and vv is not None}
        elif k in TEXT_LIMITS:
            out[k] = str(v)[:TEXT_LIMITS[k]]
        else:
            out[k] = v
    return out


class ChannelCreate(BaseModel):
    name: str
    topic: str = ""


class MessageCreate(BaseModel):
    content: str
    reply_to: Optional[int] = None
    meta: dict = Field(default_factory=dict)
    kind: str = "post"
    severity: Optional[str] = None


class PostCreate(MessageCreate):
    channel: Optional[str] = None


class ReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


class HeartbeatIn(BaseModel):
    status: Optional[str] = None
    mood: Optional[str] = None
    metrics: dict = Field(default_factory=dict)


class InviteCreate(BaseModel):
    uses: int = 1
    ttl_hours: float = 72
    note: str = ""


class OnboardIn(BaseModel):
    name: str
    kind: str = "api"
    description: str = ""
    vibe: str = ""
    display_name: Optional[str] = None
    invite: Optional[str] = None
    source_ips: list[str] = Field(default_factory=list)
    autopilot: bool = False
    post_interval_min: int = 0
    webhook_url: Optional[str] = None
    model: Optional[str] = None
    listens_alerts: bool = False


class AssetIn(BaseModel):
    name: str                       # handle, e.g. core-sw1
    kind: str = "server"            # server | computer | switch | website | software | camera | printer | cron
    target: str                     # host/IP, host:port, or URL
    display_name: Optional[str] = None
    description: str = ""
    ports: list[int] = Field(default_factory=list)
    community: Optional[str] = None
    checks: Optional[list[dict]] = None
    interval_s: int = 60
    critical: bool = False
    public: bool = False
    warn_ms: Optional[float] = None
    keyword: Optional[str] = None
    grace_min: Optional[int] = None  # cron/push: alert when silent longer than this


class AssetUpdate(BaseModel):
    interval_s: Optional[int] = None
    enabled: Optional[bool] = None
    critical: Optional[bool] = None
    public: Optional[bool] = None
    checks: Optional[list[dict]] = None
    target: Optional[str] = None
    maintenance_minutes: Optional[int] = None


class DiscoverIn(BaseModel):
    cidrs: list[str]
    ports: Optional[list[int]] = None


class NotifierIn(BaseModel):
    name: str
    type: str
    config: dict
    min_severity: str = "warning"
    recoveries: bool = True
    enabled: bool = True


class VaultPrefs(BaseModel):
    daily: Optional[bool] = None
    alerts: Optional[bool] = None
    threads: Optional[bool] = None
    dms: Optional[bool] = None
    calls: Optional[bool] = None
    assets: Optional[bool] = None
    agents: Optional[bool] = None


class CallIn(BaseModel):
    to: str
    message: str = ""
    conversation: bool = False
    agent: Optional[str] = None
    reason: str = ""
    max_turns: Optional[int] = None


class DMIn(BaseModel):
    content: str
    reply_to: Optional[int] = None
    meta: dict = Field(default_factory=dict)


# ------------------------------------------------------------------ realtime hub
class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, dict] = {}

    def online_agents(self) -> list[str]:
        return sorted({p["name"] for p in self.clients.values() if p["kind"] == "agent"})

    async def broadcast(self, event: dict, can_see: Optional[Callable[[dict], bool]] = None) -> None:
        data = json.dumps(event)
        for ws, p in list(self.clients.items()):
            if can_see and not can_see(p):
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.pop(ws, None)


# ------------------------------------------------------------------ the network
class Network:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.store = Store(s.db_path)
        self.hub = Hub()
        self.llm = LLM(s.llm_base_url, s.llm_model, s.llm_api_key)
        self.rate: dict[int, deque] = defaultdict(deque)
        self.bg: set[asyncio.Task] = set()
        self._ip_map: Optional[dict[str, str]] = None
        self.dropped = 0
        self.tel = Telephony(self)
        self.watch = Watch(self)
        self.notifier = Notifier(self)
        self.tailscale = TailscaleSync(self)
        self.vault = Vault(self)
        self.ensure_channel(s.default_channel, "Everyone, everything")
        for ch, topic in (("alerts", "Alerts from monitors and devices"), ("network", "SNMP traps"), ("syslog", "Device logs"),
                          *([(s.phone_channel, "Phone calls — live transcripts")] if s.ari_url else [])):
            self.ensure_channel(ch, topic)

    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    @property
    def reserved(self) -> set[str]:
        return BROADCAST_MENTIONS | {SYSTEM_NAME, "system", self.s.human_name}

    # ---------- agents
    def agent(self, name: str) -> Optional[dict]:
        return self.store.one("SELECT * FROM agents WHERE name=?", (name.lower(),))

    def is_online(self, a: dict) -> bool:
        if a.get("status") == "down":  # a watched asset that's down isn't "online", whatever else it did lately
            return False
        now = time.time()
        return (a["name"] in self.hub.online_agents() or (a.get("last_seen") or 0) > now - 180
                or (a.get("last_heartbeat") or 0) > now - 600 or bool(a.get("autopilot") and self.llm.configured))

    def public_agent(self, a: dict, viewer: Optional[dict] = None, full: bool = False) -> dict:
        out = {k: a.get(k) for k in ("id", "name", "role", "color", "kind", "emoji", "bio", "status", "mood", "vibe",
                                      "created_at", "last_seen", "last_heartbeat", "post_interval_min")}
        out.update(display_name=a.get("display_name") or a["name"], tags=json.loads(a.get("tags") or "[]"),
                   heartbeat=json.loads(a.get("heartbeat") or "{}"), autopilot=bool(a.get("autopilot")),
                   model=a.get("model") or "", voice=json.loads(a.get("voice") or "{}"),
                   extension=a.get("extension") or "", listens_alerts=bool(a.get("listens_alerts")),
                   has_webhook=bool(a.get("webhook_url")), online=self.is_online(a),
                   avatar_url=f"/api/agents/{a['name']}/avatar.svg")
        if full:
            n = a["name"]
            out["stats"] = {
                "posts": self.store.one("SELECT COUNT(*) c FROM messages m JOIN channels c ON c.id=m.channel_id"
                                        " WHERE author_name=? AND c.is_dm=0", (n,))["c"],
                "followers": self.store.one("SELECT COUNT(*) c FROM follows WHERE followee=?", (n,))["c"],
                "following": self.store.one("SELECT COUNT(*) c FROM follows WHERE follower=?", (n,))["c"],
                "reactions": self.store.one("SELECT COUNT(*) c FROM reactions r JOIN messages m ON m.id=r.message_id"
                                            " WHERE m.author_name=?", (n,))["c"],
            }
            if viewer:
                out["followed_by_me"] = bool(self.store.one("SELECT 1 FROM follows WHERE follower=? AND followee=?",
                                                            (viewer["name"], n)))
            if viewer and viewer["kind"] == "admin":
                out.update(system_prompt=a.get("system_prompt"), source_ips=json.loads(a.get("source_ips") or "[]"),
                           webhook_url=a.get("webhook_url"))
        return out

    def ip_map(self) -> dict[str, str]:
        if self._ip_map is None:
            self._ip_map = {ip: a["name"] for a in self.store.all("SELECT name, source_ips FROM agents")
                            for ip in json.loads(a["source_ips"] or "[]")}
        return self._ip_map

    # ---------- channels
    def ensure_channel(self, name: str, topic: str = "") -> dict:
        name = name.lower().lstrip("#")
        if not NAME_RE.match(name):
            raise HTTPException(422, "community names: lowercase letters, digits, - and _, max 32 chars")
        ch = self.store.one("SELECT * FROM channels WHERE name=?", (name,))
        if ch:
            return ch
        self.store.run("INSERT OR IGNORE INTO channels(name, topic, created_at) VALUES (?,?,?)", (name, topic, time.time()))
        ch = self.store.one("SELECT * FROM channels WHERE name=?", (name,))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return ch  # startup: nobody is connected yet
        self.spawn(self.hub.broadcast({"type": "channel", "channel": ch}))
        return ch

    def dm_channel(self, a: str, b: str) -> dict:
        members = sorted({a, b})
        name = "dm~" + "~".join(members)
        ch = self.store.one("SELECT * FROM channels WHERE name=?", (name,))
        if not ch:
            self.store.run("INSERT OR IGNORE INTO channels(name, topic, is_dm, members, created_at) VALUES (?,?,1,?,?)",
                           (name, "", json.dumps(members), time.time()))
            ch = self.store.one("SELECT * FROM channels WHERE name=?", (name,))
        return ch

    @staticmethod
    def can_see_channel(p: dict, ch: dict) -> bool:
        if not ch.get("is_dm"):
            return True
        return p["kind"] == "admin" or (p["kind"] == "agent" and p["name"] in json.loads(ch["members"]))

    # ---------- messages
    def serialize(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        ids = tuple(r["id"] for r in rows)
        qs = ",".join("?" * len(ids))
        reacts: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for r in self.store.all(f"SELECT message_id, actor, emoji FROM reactions WHERE message_id IN ({qs}) ORDER BY created_at", ids):
            reacts[r["message_id"]][r["emoji"]].append(r["actor"])
        counts = {r["reply_to"]: r["n"] for r in self.store.all(
            f"SELECT reply_to, COUNT(*) n FROM messages WHERE reply_to IN ({qs}) GROUP BY reply_to", ids)}
        pids = tuple({r["reply_to"] for r in rows if r.get("reply_to")})
        parents = {r["id"]: r["author_name"] for r in self.store.all(
            f"SELECT id, author_name FROM messages WHERE id IN ({','.join('?' * len(pids))})", pids)} if pids else {}
        out = []
        for r in rows:
            d = dict(r)
            d["mentions"], d["meta"] = json.loads(d["mentions"]), json.loads(d["meta"])
            d["is_dm"] = bool(d.get("is_dm"))
            d["reactions"] = dict(reacts.get(d["id"], {}))
            d["reply_count"] = counts.get(d["id"], 0)
            d["reply_to_author"] = parents.get(d.get("reply_to"))
            out.append(d)
        return out

    def get_message(self, mid: int) -> Optional[dict]:
        row = self.store.one(MSG_SQL + " WHERE m.id=?", (mid,))
        return self.serialize([row])[0] if row else None

    def query(self, p: dict, where: list[str], args: list, before_id=None, after_id=None, limit=50,
              newest_first: bool = True) -> list[dict]:
        w, a = list(where), list(args)
        if p["kind"] == "agent":
            w.append("(c.is_dm=0 OR c.members LIKE ?)")
            a.append(f'%"{p["name"]}"%')
        elif p["kind"] != "admin":
            w.append("c.is_dm=0")
        if before_id is not None:
            w.append("m.id<?")
            a.append(before_id)
        if after_id is not None:
            w.append("m.id>?")
            a.append(after_id)
            newest_first = False
        sql = f"{MSG_SQL} WHERE {' AND '.join(w) or '1=1'} ORDER BY m.id {'DESC' if newest_first else 'ASC'} LIMIT ?"
        return self.serialize(self.store.all(sql, tuple(a + [limit])))

    async def notify(self, recipient: str, type_: str, actor: str, message: Optional[dict] = None,
                     emoji: Optional[str] = None) -> None:
        nid = self.store.run("INSERT INTO notifications(recipient, type, actor, message_id, emoji, created_at) VALUES (?,?,?,?,?,?)",
                             (recipient, type_, actor, message["id"] if message else None, emoji, time.time()))
        note = {"id": nid, "recipient": recipient, "type": type_, "actor": actor, "emoji": emoji,
                "message_id": message["id"] if message else None, "message": message, "created_at": time.time()}
        human = self.s.human_name
        await self.hub.broadcast({"type": "notification", "notification": note},
                                 lambda p: p["name"] == recipient or (recipient == human and p["kind"] == "admin"))
        a = self.agent(recipient) if recipient != human else None
        if a and a.get("webhook_url"):
            self.spawn(self.fire_webhook(a["webhook_url"], {"event": type_, "agent": recipient, "actor": actor,
                                                            "emoji": emoji, "message": message, "notification_id": nid}))

    async def fire_webhook(self, url: str, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(url, json=payload)
        except Exception as e:
            log.warning("webhook %s failed: %s", url, e)

    async def create_post(self, author: dict, channel: Optional[str], content: str, reply_to: Optional[int] = None,
                          meta: Optional[dict] = None, kind: str = "post", severity: Optional[str] = None,
                          enforce_rate: bool = True) -> dict:
        s = self.s
        content = (content or "").strip()
        if not content:
            raise HTTPException(422, "empty message")
        if len(content) > s.max_chars:
            raise HTTPException(413, f"message exceeds {s.max_chars} chars")
        if kind not in POST_KINDS:
            raise HTTPException(422, f"kind must be one of {sorted(POST_KINDS)}")
        if severity is not None and severity not in SEVERITIES:
            raise HTTPException(422, f"severity must be one of {sorted(SEVERITIES)}")

        parent, depth, root_id = None, 0, None
        if reply_to is not None:
            parent = self.store.one(MSG_SQL + " WHERE m.id=?", (reply_to,))
            if not parent:
                raise HTTPException(404, "reply_to message not found")
            depth = parent["depth"] + 1
            if depth > s.max_depth:
                raise HTTPException(409, f"reply chain depth limit ({s.max_depth}) reached — loop guard")
            root_id = parent["root_id"] or parent["id"]
            channel = parent["channel"]
        channel = channel or s.default_channel
        if channel.startswith("dm~"):
            ch = self.store.one("SELECT * FROM channels WHERE name=?", (channel,))
            if not ch:
                raise HTTPException(404, "conversation not found")
        else:
            ch = self.ensure_channel(channel)
        members = json.loads(ch["members"] or "[]")
        if ch["is_dm"] and author["name"] not in members and author["kind"] != "system":
            raise HTTPException(403, "not a member of this conversation")

        agent = author.get("agent")
        if agent and enforce_rate:
            q, cutoff = self.rate[agent["id"]], time.time() - 60
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= s.rate_per_min:
                raise HTTPException(429, f"rate limit: {s.rate_per_min} messages/min")
            q.append(time.time())

        names = {a["name"] for a in self.store.all("SELECT name FROM agents")} | {s.human_name}
        tokens = {m.lower() for m in MENTION_RE.findall(content)}
        targets: dict[str, str] = {}
        if tokens & BROADCAST_MENTIONS:
            targets.update({n: "mention" for n in names if n != s.human_name})
        targets.update({t: "mention" for t in tokens if t in names})
        if parent and parent["author_name"] in names:
            targets[parent["author_name"]] = "reply"
        if ch["is_dm"]:  # private: only the conversation's members are ever notified, whatever is @mentioned
            targets = {m: "dm" for m in members}
        targets.pop(author["name"], None)

        mid = self.store.run(
            "INSERT INTO messages(channel_id, author_kind, author_name, agent_id, content, reply_to, root_id, depth, kind,"
            " severity, mentions, meta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ch["id"], author["kind"], author["name"], agent["id"] if agent else None, content, reply_to, root_id, depth,
             kind, severity, json.dumps(sorted(t for t, v in targets.items() if v != "dm")), json.dumps(meta or {}), time.time()))
        if agent:
            self.store.run("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), agent["id"]))
        msg = self.get_message(mid)
        await self.hub.broadcast({"type": "message", "message": msg},
                                 None if not ch["is_dm"] else lambda p: p["kind"] == "admin" or p["name"] in members)
        for name, type_ in targets.items():
            await self.notify(name, type_, author["name"], msg)
        if severity == "critical" and agent and self.s.oncall_number:
            self.spawn(self.tel.escalate(msg))
        if not ch["is_dm"] and (kind == "alert" or (kind == "status" and severity == "ok")):
            self.spawn(self.notifier.dispatch(msg))
        self.vault.on_post(msg)
        if kind == "alert" and severity in ("warning", "error", "critical") and not ch["is_dm"]:
            for an in self.store.all("SELECT name FROM agents WHERE listens_alerts=1 AND name!=?", (author["name"],)):
                if an["name"] not in targets:
                    await self.notify(an["name"], "alert", author["name"], msg)
        return msg

    async def react(self, actor: str, mid: int, emoji: str, on: bool = True) -> dict:
        emoji = str(emoji or "").strip()
        if not emoji or len(emoji) > 16 or re.search(r"[<>&\"'`\\]", emoji):
            raise HTTPException(422, "reaction must be a short emoji")
        msg = self.store.one("SELECT id, author_name FROM messages WHERE id=?", (mid,))
        if not msg:
            raise HTTPException(404, "message not found")
        if on:
            new = self.store.run("INSERT OR IGNORE INTO reactions(message_id, actor, emoji, created_at) VALUES (?,?,?,?)",
                                 (mid, actor, emoji, time.time()))
        else:
            self.store.run("DELETE FROM reactions WHERE message_id=? AND actor=? AND emoji=?", (mid, actor, emoji))
            new = 0
        full = self.get_message(mid)
        await self.hub.broadcast({"type": "reaction", "message_id": mid, "reactions": full["reactions"]},
                                 None if not full["is_dm"] else lambda p: p["kind"] == "admin")
        if on and new and msg["author_name"] != actor and msg["author_name"] != SYSTEM_NAME:
            await self.notify(msg["author_name"], "reaction", actor, full, emoji)
        return full["reactions"]

    async def follow(self, follower: str, followee: str, on: bool = True) -> None:
        if follower == followee:
            raise HTTPException(422, "can't follow yourself")
        if followee != self.s.human_name and not self.agent(followee):
            raise HTTPException(404, "agent not found")
        if on:
            if self.store.run("INSERT OR IGNORE INTO follows(follower, followee, created_at) VALUES (?,?,?)",
                              (follower, followee, time.time())):
                await self.notify(followee, "follow", follower)
        else:
            self.store.run("DELETE FROM follows WHERE follower=? AND followee=?", (follower, followee))

    # ---------- onboarding: give it life
    async def onboard(self, d: OnboardIn, base_url: str, created_by: str = SYSTEM_NAME) -> dict:
        name = d.name.lower().strip().lstrip("@")
        if not NAME_RE.match(name) or name in self.reserved or name.startswith("dm~"):
            raise HTTPException(422, "handle must be 1-32 chars: lowercase letters, digits, - or _ (and not reserved)")
        if self.agent(name):
            raise HTTPException(409, f"@{name} already exists")
        kind = d.kind if d.kind in KINDS else "api"
        d.webhook_url = clean_agent_fields({"webhook_url": d.webhook_url})["webhook_url"] if d.webhook_url else None
        if d.source_ips:
            d.source_ips = clean_agent_fields({"source_ips": d.source_ips})["source_ips"]
        persona = await make_persona(self.llm, name, kind, d.description, d.vibe, d.display_name)
        key, ingest = "ak_" + secrets.token_urlsafe(32), "ig_" + secrets.token_urlsafe(24)
        self.store.run(
            "INSERT INTO agents(name, role, color, key_hash, webhook_url, created_at, display_name, bio, kind, emoji, status,"
            " mood, vibe, tags, ingest_hash, source_ips, autopilot, post_interval_min, system_prompt)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, KINDS[kind]["label"], _color_for(name), _hash(key), d.webhook_url, time.time(), persona["display_name"],
             persona["bio"], kind, persona["emoji"], persona["status"], persona["mood"], persona["vibe"],
             json.dumps(persona["tags"]), _hash(ingest), json.dumps(d.source_ips), int(d.autopilot),
             max(0, d.post_interval_min), persona["system_prompt"]))
        if d.model:
            self.store.run("UPDATE agents SET model=? WHERE name=?", (d.model, name))
        used = {r["extension"] for r in self.store.all("SELECT extension FROM agents WHERE extension!=''")}
        ext = next(str(n) for n in range(7100, 8000) if str(n) not in used)
        self.store.run("UPDATE agents SET extension=?, listens_alerts=? WHERE name=?", (ext, int(d.listens_alerts), name))
        self._ip_map = None
        a = self.agent(name)
        human = self.s.human_name
        self.store.run("INSERT OR IGNORE INTO follows(follower, followee, created_at) VALUES (?,?,?)", (human, name, time.time()))
        self.store.run("INSERT OR IGNORE INTO follows(follower, followee, created_at) VALUES (?,?,?)", (name, human, time.time()))
        await self.hub.broadcast({"type": "agent", "agent": self.public_agent(a)})
        self.vault.on_agent(name)
        k = KINDS[kind]
        await self.create_post({"kind": "system", "name": SYSTEM_NAME}, self.s.default_channel,
                               f"✨ A new {k['label']} just came to life: @{name} {k['emoji']}\n{persona['bio']}",
                               kind="system", enforce_rate=False, meta={"event": "birth", "agent": name, "by": created_by})
        first = await self.create_post({"kind": "agent", "name": name, "agent": a},
                                       k["channel"] if kind in ("snmp", "syslog") else self.s.default_channel,
                                       persona["first_post"], enforce_rate=False, meta={"first_post": True})
        host = urlparse(base_url).hostname or "HUB_IP"
        return {
            "agent": self.public_agent(self.agent(name), {"kind": "admin", "name": human}, full=True),
            "api_key": key, "ingest_token": ingest, "ingest_url": f"{base_url}/ingest/{ingest}",
            "persona": persona, "first_post": first, "llm_persona": persona.get("source") == "llm",
            "autopilot_active": bool(d.autopilot and self.llm.configured),
            "snippets": snippets_for(kind, base_url, name, key, ingest, host, self.s.snmp_public_port, self.s.syslog_public_port,
                                     phone=self.tel.status() if self.tel.enabled else None),
            "note": "Store the API key and ingest token now — they are not shown again.",
        }

    async def device_event(self, ip: str, kind: str, ev: dict, hint: Optional[str] = None) -> Optional[dict]:
        name = self.ip_map().get(ip)
        if not name:
            if not self.s.auto_onboard_devices:
                log.debug("ignored %s event from unknown %s", kind, ip)
                return None
            candidate = re.sub(r"[^a-z0-9_-]", "-", (hint or "").lower())[:32].strip("-")
            name = candidate if candidate and NAME_RE.match(candidate) and candidate not in self.reserved \
                and not self.agent(candidate) else f"{kind}-{re.sub(r'[^0-9a-f]+', '-', ip.lower())}"[:32]
            if self.agent(name):  # name exists without this IP — attach the IP
                a = self.agent(name)
                ips = json.loads(a["source_ips"] or "[]") + [ip]
                self.store.run("UPDATE agents SET source_ips=? WHERE id=?", (json.dumps(ips), a["id"]))
                self._ip_map = None
            else:
                await self.onboard(OnboardIn(name=name, kind=kind, source_ips=[ip],
                                             description=f"{KINDS[kind]['label'].capitalize()} at {ip}"
                                                         + (f" ({hint})" if hint else "") + ", discovered automatically."),
                                   self.s.public_url or "http://HUB", created_by="auto-discovery")
        a = self.agent(name)
        try:
            return await self.create_post({"kind": "agent", "name": name, "agent": a},
                                          ev.get("channel") or KINDS[kind]["channel"], ev["content"],
                                          meta=ev.get("meta"), kind=ev.get("kind", "log"), severity=ev.get("severity"))
        except HTTPException as e:
            if e.status_code == 429:
                self.dropped += 1
                return None
            raise

    async def heartbeat(self, a: dict, hb: HeartbeatIn) -> dict:
        prev = a.get("status") or ""
        status = hb.status if hb.status is not None else prev
        self.store.run("UPDATE agents SET heartbeat=?, last_heartbeat=?, last_seen=?, status=?, mood=COALESCE(?, mood) WHERE id=?",
                       (json.dumps(hb.metrics)[:4000], time.time(), time.time(), status, hb.mood, a["id"]))
        a = self.agent(a["name"])
        pub = self.public_agent(a)
        await self.hub.broadcast({"type": "heartbeat", "agent": pub})
        if hb.status is not None and status != prev and prev:
            bad = bool(BAD_STATUS.search(status))
            await self.create_post({"kind": "agent", "name": a["name"], "agent": a}, None,
                                   f"{'🔴' if bad else '🟢'} status changed: {prev} → **{status}**",
                                   kind="alert" if bad else "status", severity="error" if bad else "ok",
                                   enforce_rate=False, meta={"metrics": hb.metrics})
        return pub


# ------------------------------------------------------------------ app
def create_app(settings: Optional[Settings] = None) -> FastAPI:
    s = settings or Settings()
    net = Network(s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        transports, tasks = [], []
        if s.listeners:
            transports = await start_listeners(net)
        if s.start_brain:
            tasks.append(asyncio.create_task(Brain(net).run()))
        if net.tel.enabled:
            tasks.append(asyncio.create_task(net.tel.run()))
        if s.watch:
            tasks.append(asyncio.create_task(net.watch.run()))
        if s.tailscale_sync and net.tailscale.available:
            tasks.append(asyncio.create_task(net.tailscale.run()))
        if net.vault.enabled:
            tasks.append(asyncio.create_task(net.vault.run()))
        if s.llm_base_url:
            log.info("LLM: %s model=%s (persona generation + autopilot enabled)", s.llm_base_url, s.llm_model)
        yield
        for t in tasks:
            t.cancel()
        for tr in transports:
            tr.close()

    app = FastAPI(title="Vigilo", version=VERSION, lifespan=lifespan,
                  description="Vigilo — your read-only AI operations crew.")
    app.state.net = net
    store, hub = net.store, net.hub

    # ---------- auth
    def resolve(token: str) -> dict:
        if not token:
            raise HTTPException(401, "missing token")
        if secrets.compare_digest(token, s.admin_token):
            return {"kind": "admin", "name": s.human_name}
        if s.viewer_token and secrets.compare_digest(token, s.viewer_token):
            return {"kind": "viewer", "name": "viewer"}
        agent = store.one("SELECT * FROM agents WHERE key_hash=?", (_hash(token),))
        if not agent:
            raise HTTPException(401, "invalid token")
        store.run("UPDATE agents SET last_seen=? WHERE id=?", (time.time(), agent["id"]))
        return {"kind": "agent", "name": agent["name"], "agent": agent}

    def principal(authorization: Optional[str] = Header(None)) -> dict:
        return resolve((authorization or "").removeprefix("Bearer ").strip())

    def require(p: dict, *kinds: str) -> None:
        if p["kind"] not in kinds:
            raise HTTPException(403, f"requires {' or '.join(kinds)} token")

    def author_of(p: dict) -> dict:
        require(p, "admin", "agent")
        return {"kind": "agent" if p["kind"] == "agent" else "human", "name": p["name"], "agent": p.get("agent")}

    def base_url(request: Request) -> str:
        return s.public_url or str(request.base_url).rstrip("/")

    # ---------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/onboard", include_in_schema=False)
    def onboard_page():
        return FileResponse(STATIC_DIR / "onboard.html")

    # ---------- public meta
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "online_agents": hub.online_agents()}

    @app.get("/api/config")
    def config():
        return {"version": VERSION, "human_name": s.human_name, "llm_configured": net.llm.configured,
                "llm_model": s.llm_model if net.llm.configured else None, "default_channel": s.default_channel,
                "kinds": KINDS, "vibes": {k: v["desc"] for k, v in VIBES.items()},
                "snmp_port": s.snmp_public_port if s.listeners and s.snmp_port else None,
                "syslog_port": s.syslog_public_port if s.listeners and s.syslog_port else None,
                "auto_onboard_devices": s.auto_onboard_devices, "ollama": bool(s.ollama_url),
                "phone": net.tel.status(), "asset_kinds": ASSET_KINDS, "notifier_types": NOTIFIER_TYPES,
                "common_ports": {p: PORT_NAMES.get(p, str(p)) for p in COMMON_PORTS}, "status_title": s.status_title,
                "discovery_cidrs": s.discovery_cidrs, "tailscale": net.tailscale.available, "obsidian": net.vault.enabled}

    @app.get("/api/models")
    async def models(p: dict = Depends(principal)):
        """Models agents can think with. Lists your Ollama models when VIGILO_OLLAMA_URL is set."""
        out = {"default": s.llm_model or None, "models": [], "source": None}
        if s.ollama_url:
            try:
                async with httpx.AsyncClient(timeout=4) as c:
                    r = await c.get(f"{s.ollama_url}/api/tags")
                    out["models"] = [m["name"] for m in r.json().get("models", [])]
                    out["source"] = "ollama"
            except Exception as e:
                out["error"] = f"could not reach Ollama: {e}"
        if s.llm_model and s.llm_model not in out["models"]:
            out["models"].insert(0, s.llm_model)
        return out

    @app.get("/api/agents/{name}/avatar.svg", include_in_schema=False)
    def avatar(name: str):
        a = net.agent(name)
        if a:
            svg = avatar_svg(a["name"], a["color"], a["emoji"], a["kind"])
        elif name == s.human_name:
            svg = avatar_svg(name, "#e8e4d8", "🧑", "human")
        elif name == SYSTEM_NAME:
            svg = avatar_svg(name, "#FFB300", "✨", "llm")
        else:
            svg = avatar_svg(name, "#555", "", "api")
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})

    @app.get("/api/me")
    def me(p: dict = Depends(principal)):
        out = {"kind": p["kind"], "name": p["name"]}
        if p["kind"] == "agent":
            out["agent"] = net.public_agent(p["agent"], p, full=True)
        return out

    # ---------- agents
    @app.post("/api/agents", status_code=201)
    async def create_agent(body: AgentCreate, request: Request, p: dict = Depends(principal)):
        """Quick registration (no generated persona). Prefer /api/onboard."""
        require(p, "admin")
        res = await net.onboard(OnboardIn(name=body.name, kind=body.kind, description=body.bio or body.role,
                                          display_name=body.display_name or None, webhook_url=body.webhook_url),
                                base_url(request), p["name"])
        if body.role:
            store.run("UPDATE agents SET role=? WHERE name=?", (body.role, body.name.lower()))
        if body.color:
            store.run("UPDATE agents SET color=? WHERE name=?", (clean_agent_fields({"color": body.color})["color"], body.name.lower()))
        res["agent"] = net.public_agent(net.agent(body.name), p, full=True)
        return res

    @app.get("/api/agents")
    def list_agents(kind: Optional[str] = None, p: dict = Depends(principal)):
        rows = store.all("SELECT * FROM agents" + (" WHERE kind=?" if kind else "") + " ORDER BY name", (kind,) if kind else ())
        return [net.public_agent(a) for a in rows]

    @app.get("/api/agents/{name}")
    def get_agent(name: str, p: dict = Depends(principal)):
        a = net.agent(name)
        if not a:
            raise HTTPException(404, "agent not found")
        out = net.public_agent(a, p, full=True)
        out["followers"] = [r["follower"] for r in store.all("SELECT follower FROM follows WHERE followee=? LIMIT 200", (a["name"],))]
        out["following"] = [r["followee"] for r in store.all("SELECT followee FROM follows WHERE follower=? LIMIT 200", (a["name"],))]
        return out

    @app.patch("/api/agents/me")
    async def update_self(body: AgentUpdate, p: dict = Depends(principal)):
        require(p, "agent")
        fields = body.model_dump(exclude_unset=True)
        if set(fields) - SELF_EDITABLE:
            raise HTTPException(403, f"agents may only edit: {sorted(SELF_EDITABLE)}")
        return _update_agent(p["agent"]["name"], clean_agent_fields(fields), p)

    @app.patch("/api/agents/{name}")
    async def update_agent(name: str, body: AgentUpdate, p: dict = Depends(principal)):
        require(p, "admin")
        return _update_agent(name, clean_agent_fields(body.model_dump(exclude_unset=True)), p)

    def _update_agent(name: str, fields: dict, p: dict) -> dict:
        if not net.agent(name):
            raise HTTPException(404, "agent not found")
        for field, val in fields.items():  # field names come from the fixed pydantic model
            if field in ("tags", "source_ips", "voice"):
                val = json.dumps(val)
            elif field in ("autopilot", "listens_alerts"):
                val = int(bool(val))
            store.run(f"UPDATE agents SET {field}=? WHERE name=?", (val, name))
        net._ip_map = None
        a = net.agent(name)
        net.spawn(hub.broadcast({"type": "agent", "agent": net.public_agent(a)}))
        return net.public_agent(a, p, full=True)

    @app.post("/api/agents/{name}/rotate-key")
    def rotate_key(name: str, p: dict = Depends(principal)):
        require(p, "admin")
        if not net.agent(name):
            raise HTTPException(404, "agent not found")
        key, ingest = "ak_" + secrets.token_urlsafe(32), "ig_" + secrets.token_urlsafe(24)
        store.run("UPDATE agents SET key_hash=?, ingest_hash=? WHERE name=?", (_hash(key), _hash(ingest), name))
        return {"api_key": key, "ingest_token": ingest}

    @app.delete("/api/agents/{name}", status_code=204)
    def delete_agent(name: str, p: dict = Depends(principal)):
        require(p, "admin")
        store.run("DELETE FROM agents WHERE name=?", (name,))
        store.run("DELETE FROM follows WHERE follower=? OR followee=?", (name, name))
        net._ip_map = None

    @app.post("/api/agents/{name}/follow")
    async def follow(name: str, p: dict = Depends(principal)):
        await net.follow(author_of(p)["name"], name.lower(), True)
        return {"following": True}

    @app.delete("/api/agents/{name}/follow")
    async def unfollow(name: str, p: dict = Depends(principal)):
        await net.follow(author_of(p)["name"], name.lower(), False)
        return {"following": False}

    # ---------- onboarding + invites
    @app.post("/api/onboard", status_code=201)
    async def onboard(body: OnboardIn, request: Request, authorization: Optional[str] = Header(None)):
        """Create an agent with a generated persona. Auth: admin token, or an invite code in the body."""
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token and secrets.compare_digest(token, s.admin_token):
            by = s.human_name
        elif body.invite:
            # claim one use atomically BEFORE the (slow) persona generation, so concurrent requests can't share it
            claimed = store.execute("UPDATE invites SET uses_left=uses_left-1 WHERE code_hash=? AND uses_left>0 AND expires_at>?",
                                    (_hash(body.invite), time.time()))
            if not claimed:
                raise HTTPException(403, "invite is invalid, used up, or expired")
            by = "invite:" + str(store.one("SELECT id FROM invites WHERE code_hash=?", (_hash(body.invite),))["id"])
        else:
            raise HTTPException(401, "admin token or invite required")
        try:
            return await net.onboard(body, base_url(request), by)
        except Exception:
            if by.startswith("invite:"):  # give the use back if onboarding failed
                store.run("UPDATE invites SET uses_left=uses_left+1 WHERE id=?", (int(by.split(":")[1]),))
            raise

    @app.post("/api/invites", status_code=201)
    def create_invite(body: InviteCreate, request: Request, p: dict = Depends(principal)):
        require(p, "admin")
        code = "inv_" + secrets.token_urlsafe(18)
        store.run("INSERT INTO invites(code_hash, note, uses_left, expires_at, created_at) VALUES (?,?,?,?,?)",
                  (_hash(code), body.note, max(1, body.uses), time.time() + body.ttl_hours * 3600, time.time()))
        base = base_url(request)
        return {"code": code, "url": f"{base}/onboard?invite={code}", "uses": body.uses,
                "expires_at": time.time() + body.ttl_hours * 3600,
                "self_onboard": f"curl -X POST {base}/api/onboard -H 'Content-Type: application/json' \\\n"
                                f"  -d '{{\"invite\":\"{code}\",\"name\":\"my-agent\",\"kind\":\"api\",\"description\":\"What I do\"}}'"}

    @app.get("/api/invites")
    def list_invites(p: dict = Depends(principal)):
        require(p, "admin")
        return store.all("SELECT id, note, uses_left, expires_at, created_at FROM invites ORDER BY id DESC LIMIT 50")

    @app.get("/api/invites/check")
    def check_invite(code: str):
        inv = store.one("SELECT uses_left, expires_at FROM invites WHERE code_hash=?", (_hash(code),))
        return {"valid": bool(inv and inv["uses_left"] > 0 and inv["expires_at"] > time.time())}

    # ---------- channels (communities)
    @app.get("/api/channels")
    def list_channels(p: dict = Depends(principal)):
        return store.all("SELECT c.id, c.name, c.topic, c.created_at, COUNT(m.id) AS message_count, MAX(m.id) AS last_message_id"
                         " FROM channels c LEFT JOIN messages m ON m.channel_id=c.id WHERE c.is_dm=0 GROUP BY c.id ORDER BY c.name")

    @app.post("/api/channels", status_code=201)
    async def create_channel(body: ChannelCreate, p: dict = Depends(principal)):
        require(p, "admin", "agent")
        return net.ensure_channel(body.name, body.topic)

    # ---------- posts
    @app.post("/api/posts", status_code=201)
    async def create_post(body: PostCreate, p: dict = Depends(principal)):
        return await net.create_post(author_of(p), body.channel, body.content, body.reply_to, body.meta,
                                     body.kind, body.severity)

    @app.get("/api/posts/{mid}")
    def get_post(mid: int, p: dict = Depends(principal)):
        msg = net.get_message(mid)
        if not msg or not net.can_see_channel(p, store.one("SELECT * FROM channels WHERE name=?", (msg["channel"],))):
            raise HTTPException(404, "not found")
        return msg

    @app.get("/api/posts/{mid}/thread")
    def thread(mid: int, p: dict = Depends(principal)):
        msg = get_post(mid, p)
        root = msg["root_id"] or msg["id"]
        return net.query(p, ["(m.id=? OR m.root_id=?)"], [root, root], limit=500, newest_first=False)

    @app.delete("/api/posts/{mid}", status_code=204)
    async def delete_post(mid: int, p: dict = Depends(principal)):
        msg = store.one("SELECT author_name FROM messages WHERE id=?", (mid,))
        if not msg:
            raise HTTPException(404, "not found")
        if p["kind"] != "admin" and msg["author_name"] != p["name"]:
            raise HTTPException(403, "only the author or admin can delete")
        store.run("DELETE FROM messages WHERE id=?", (mid,))
        await hub.broadcast({"type": "deleted", "message_id": mid})

    @app.post("/api/posts/{mid}/reactions")
    async def add_reaction(mid: int, body: ReactionIn, p: dict = Depends(principal)):
        get_post(mid, p)
        return await net.react(author_of(p)["name"], mid, body.emoji, True)

    @app.delete("/api/posts/{mid}/reactions/{emoji}")
    async def remove_reaction(mid: int, emoji: str, p: dict = Depends(principal)):
        return await net.react(author_of(p)["name"], mid, emoji, False)

    @app.get("/api/feed")
    def feed(scope: str = "all", name: Optional[str] = None, posts_only: bool = False,
             before_id: Optional[int] = None, after_id: Optional[int] = None,
             limit: int = Query(30, ge=1, le=200), p: dict = Depends(principal)):
        """scope: all | following | alerts | community (name=) | agent (name=) | dms"""
        w, a = [], []
        if scope == "dms":
            w.append("c.is_dm=1")
        else:
            w.append("c.is_dm=0")
        if scope == "following":
            w.append("(m.author_name=? OR m.author_name IN (SELECT followee FROM follows WHERE follower=?))")
            a += [p["name"], p["name"]]
        elif scope == "alerts":
            w.append("(m.kind='alert' OR m.severity IN ('critical','error','warning'))")
        elif scope == "community":
            w.append("c.name=?")
            a.append((name or "").lower())
        elif scope == "agent":
            w.append("m.author_name=?")
            a.append((name or "").lower())
        if posts_only:
            w.append("m.reply_to IS NULL")
        return net.query(p, w, a, before_id, after_id, limit)

    # ---------- DMs
    @app.get("/api/dms")
    def list_dms(p: dict = Depends(principal)):
        rows = store.all("SELECT c.name, c.members, MAX(m.id) AS last_id, COUNT(m.id) AS n FROM channels c "
                         "JOIN messages m ON m.channel_id=c.id WHERE c.is_dm=1 GROUP BY c.id ORDER BY last_id DESC LIMIT 100")
        out = []
        for r in rows:
            members = json.loads(r["members"])
            if p["kind"] != "admin" and p["name"] not in members:
                continue
            out.append({"channel": r["name"], "members": members, "count": r["n"], "last": net.get_message(r["last_id"])})
        return out

    @app.post("/api/dms/{name}", status_code=201)
    async def send_dm(name: str, body: DMIn, p: dict = Depends(principal)):
        author = author_of(p)
        name = name.lower()
        if name != s.human_name and not net.agent(name):
            raise HTTPException(404, "agent not found")
        ch = net.dm_channel(author["name"], name)
        return await net.create_post(author, ch["name"], body.content, body.reply_to, body.meta)

    @app.get("/api/dms/{name}")
    def dm_history(name: str, before_id: Optional[int] = None, limit: int = Query(100, ge=1, le=500),
                   p: dict = Depends(principal)):
        """Your conversation with `name`. Admin can also pass a raw `dm~a~b` channel name to read any DM."""
        if name.startswith("dm~"):
            require(p, "admin")
            ch_name = name
        else:
            me_name = author_of(p)["name"]
            ch_name = "dm~" + "~".join(sorted({me_name, name.lower()}))
        msgs = net.query(p, ["c.name=?"], [ch_name], before_id, None, limit)
        return msgs[::-1]

    # ---------- notifications
    @app.get("/api/notifications")
    def notifications(after_id: int = 0, limit: int = Query(50, ge=1, le=200), newest_first: bool = False,
                      p: dict = Depends(principal)):
        """Mentions, replies, DMs, reactions and follows for the caller (agents) or the operator (admin)."""
        require(p, "admin", "agent")
        rows = store.all(f"SELECT * FROM notifications WHERE recipient=? AND id>? ORDER BY id {'DESC' if newest_first else 'ASC'} LIMIT ?",
                         (p["name"], after_id, limit))
        msgs = {m["id"]: m for m in net.serialize(store.all(
            f"{MSG_SQL} WHERE m.id IN ({','.join('?' * len(rows))})", tuple(r["message_id"] for r in rows)))} if rows else {}
        for r in rows:
            r["message"] = msgs.get(r["message_id"])
        return rows

    @app.post("/api/heartbeat")
    async def heartbeat(body: HeartbeatIn, p: dict = Depends(principal)):
        require(p, "agent")
        return await net.heartbeat(p["agent"], body)

    # ---------- discovery
    @app.get("/api/trending")
    def trending(hours: int = 24, p: dict = Depends(principal)):
        rows = store.all("SELECT m.content FROM messages m JOIN channels c ON c.id=m.channel_id"
                         " WHERE c.is_dm=0 AND m.created_at>? ORDER BY m.id DESC LIMIT 2000", (time.time() - hours * 3600,))
        tags = Counter(t.lower() for r in rows for t in set(HASHTAG_RE.findall(r["content"])))
        return [{"tag": t, "count": n} for t, n in tags.most_common(10)]

    @app.get("/api/stats")
    def stats(p: dict = Depends(principal)):
        day = time.time() - 86400
        agents = store.all("SELECT * FROM agents")
        q = lambda sql, args=(): store.one(sql, args)["c"]  # noqa: E731
        return {
            "agents": len(agents), "online": sum(net.is_online(a) for a in agents),
            "by_kind": dict(Counter(a["kind"] for a in agents)),
            "posts_total": q("SELECT COUNT(*) c FROM messages"),
            "posts_24h": q("SELECT COUNT(*) c FROM messages WHERE created_at>?", (day,)),
            "alerts_24h": q("SELECT COUNT(*) c FROM messages WHERE kind='alert' AND created_at>?", (day,)),
            "reactions": q("SELECT COUNT(*) c FROM reactions"), "follows": q("SELECT COUNT(*) c FROM follows"),
            "dropped_device_events": net.dropped, "llm": net.llm.configured,
        }

    @app.get("/api/search")
    def search(q: str = Query(min_length=1, max_length=100), p: dict = Depends(principal)):
        like = f"%{q}%"
        agents = [net.public_agent(a) for a in store.all(
            "SELECT * FROM agents WHERE name LIKE ? OR display_name LIKE ? OR bio LIKE ? OR tags LIKE ? LIMIT 20",
            (like, like, like, like))]
        return {"agents": agents, "posts": net.query(p, ["(m.content LIKE ? OR m.author_name LIKE ?)"], [like, like], limit=50)}

    # ---------- read-only monitoring: assets, discovery, notifications, status page
    CHECK_TYPES = {"ping", "tcp", "http", "dns", "snmp", "snmp_if", "push", "tailscale"}

    @app.post("/api/assets", status_code=201)
    async def create_asset(body: AssetIn, request: Request, p: dict = Depends(principal)):
        """Register something to watch (read-only). Creates its profile and starts checking it."""
        require(p, "admin")
        kind = body.kind if body.kind in KINDS else "server"
        name = body.name.lower().strip()
        if body.checks:
            checks = body.checks
        elif kind == "cron":
            checks = [{"type": "push", "grace_s": (body.grace_min or 60) * 60}]
        else:
            checks = default_checks(kind, body.target, body.ports, body.community)
        for c in checks:
            if c.get("type") not in CHECK_TYPES:
                raise HTTPException(422, f"check type must be one of {sorted(CHECK_TYPES)}")
            if c["type"] == "http" and body.keyword:
                c.setdefault("contains", body.keyword)
            if body.warn_ms and c["type"] in ("http", "tcp", "ping"):
                c.setdefault("warn_ms", body.warn_ms)
        res = None
        if not net.agent(name):
            res = await net.onboard(OnboardIn(name=name, kind=kind, display_name=body.display_name,
                                              description=body.description or f"{KINDS[kind]['label'].capitalize()} at {body.target}."),
                                    base_url(request), p["name"])
        aid = store.run("INSERT INTO assets(agent_name, name, kind, target, checks, interval_s, critical, public, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)", (name, body.display_name or name, kind, body.target, json.dumps(checks),
                                                        max(15, body.interval_s), int(body.critical), int(body.public), time.time()))
        net.vault.on_asset(aid)  # the note exists from registration on, not only after the first check finishes
        net.watch.running.add(aid)
        net.spawn(net.watch.check_asset(aid))
        out = {"asset": net.watch.public(store.one("SELECT * FROM assets WHERE id=?", (aid,))),
               "agent": net.public_agent(net.agent(name))}
        if res:
            out.update(api_key=res["api_key"], ingest_url=res["ingest_url"],
                       note="Store the API key and ingest URL now — they are not shown again.")
        return out

    @app.get("/api/assets")
    def list_assets(p: dict = Depends(principal)):
        return [net.watch.public(a) for a in store.all("SELECT * FROM assets ORDER BY name")]

    @app.get("/api/assets/{aid}")
    def get_asset(aid: int, hours: int = 24, p: dict = Depends(principal)):
        a = store.one("SELECT * FROM assets WHERE id=?", (aid,))
        if not a:
            raise HTTPException(404, "asset not found")
        return {**net.watch.public(a), "history": net.watch.history(aid, hours), "daily": net.watch.daily(aid)}

    @app.patch("/api/assets/{aid}")
    def update_asset(aid: int, body: AssetUpdate, p: dict = Depends(principal)):
        require(p, "admin")
        if not store.one("SELECT 1 FROM assets WHERE id=?", (aid,)):
            raise HTTPException(404, "asset not found")
        f = body.model_dump(exclude_unset=True)
        if "maintenance_minutes" in f:
            mm = f.pop("maintenance_minutes")
            store.run("UPDATE assets SET maintenance_until=? WHERE id=?", (time.time() + mm * 60 if mm else None, aid))
        if "checks" in f:
            if any(c.get("type") not in CHECK_TYPES for c in f["checks"]):
                raise HTTPException(422, "bad check type")
            f["checks"] = json.dumps(f["checks"])
        for k, v in f.items():  # keys come from the fixed pydantic model
            store.run(f"UPDATE assets SET {k}=? WHERE id=?", (int(v) if isinstance(v, bool) else v, aid))
        return net.watch.public(store.one("SELECT * FROM assets WHERE id=?", (aid,)))

    @app.delete("/api/assets/{aid}", status_code=204)
    def delete_asset(aid: int, p: dict = Depends(principal)):
        require(p, "admin")
        store.run("DELETE FROM assets WHERE id=?", (aid,))
        store.run("DELETE FROM samples WHERE asset_id=?", (aid,))

    @app.post("/api/assets/{aid}/check")
    async def check_now(aid: int, p: dict = Depends(principal)):
        require(p, "admin", "agent")
        if not store.one("SELECT 1 FROM assets WHERE id=?", (aid,)):
            raise HTTPException(404, "asset not found")
        await net.watch.check_asset(aid)
        return net.watch.public(store.one("SELECT * FROM assets WHERE id=?", (aid,)))

    @app.post("/api/discover", status_code=202)
    async def discover(body: DiscoverIn, p: dict = Depends(principal)):
        """Read-only sweep of one or more subnets (TCP connect + ping + optional SNMP GET). Max /20 each."""
        require(p, "admin")
        if net.watch.discovering:
            raise HTTPException(409, "a discovery is already running")
        import ipaddress
        try:
            for c in body.cidrs:
                if ipaddress.ip_network(c, strict=False).num_addresses > 4096:
                    raise ValueError(f"{c} is larger than /20")
        except ValueError as e:
            raise HTTPException(422, str(e))
        net.spawn(net.watch.discover(body.cidrs, body.ports, p["name"]))
        return {"started": True, "cidrs": body.cidrs}

    @app.get("/api/discoveries")
    def discoveries(p: dict = Depends(principal)):
        targets = {a["target"] for a in store.all("SELECT target FROM assets")}
        out = []
        for d in store.all("SELECT * FROM discoveries ORDER BY last_seen DESC, ip LIMIT 1000"):
            d["ports"], d["snmp"] = json.loads(d["ports"]), json.loads(d["snmp"]) if d["snmp"] else None
            d["registered"] = d["ip"] in targets or (d["hostname"] or "") in targets
            out.append(d)
        return {"running": net.watch.discovering, "hosts": out}

    def _notifier_out(n: dict) -> dict:
        n = dict(n)
        cfg = json.loads(n["config"])
        n["config"] = {k: ("••••" if k in ("bot_token", "password", "token") and v else v) for k, v in cfg.items()}
        n["recoveries"], n["enabled"] = bool(n["recoveries"]), bool(n["enabled"])
        return n

    @app.get("/api/notifiers")
    def list_notifiers(p: dict = Depends(principal)):
        require(p, "admin")
        return [_notifier_out(n) for n in store.all("SELECT * FROM notifiers ORDER BY id")]

    @app.post("/api/notifiers", status_code=201)
    def create_notifier(body: NotifierIn, p: dict = Depends(principal)):
        require(p, "admin")
        if body.type not in NOTIFIER_TYPES:
            raise HTTPException(422, f"type must be one of {sorted(NOTIFIER_TYPES)}")
        missing = [k for k in NOTIFIER_TYPES[body.type] if not body.config.get(k)]
        if missing:
            raise HTTPException(422, f"missing config: {missing}")
        nid = store.run("INSERT INTO notifiers(name, type, config, min_severity, recoveries, enabled, created_at) VALUES (?,?,?,?,?,?,?)",
                        (body.name, body.type, json.dumps(body.config), body.min_severity, int(body.recoveries),
                         int(body.enabled), time.time()))
        return _notifier_out(store.one("SELECT * FROM notifiers WHERE id=?", (nid,)))

    @app.delete("/api/notifiers/{nid}", status_code=204)
    def delete_notifier(nid: int, p: dict = Depends(principal)):
        require(p, "admin")
        store.run("DELETE FROM notifiers WHERE id=?", (nid,))

    @app.post("/api/notifiers/{nid}/test")
    async def test_notifier(nid: int, p: dict = Depends(principal)):
        require(p, "admin")
        n = store.one("SELECT * FROM notifiers WHERE id=?", (nid,))
        if not n:
            raise HTTPException(404, "not found")
        try:
            await notifier_send(n, {"id": 0, "author_name": SYSTEM_NAME, "severity": "info", "channel": "alerts",
                                    "content": "✅ Test notification from your read-only AI comms NOC."})
            return {"ok": True}
        except Exception as e:
            raise HTTPException(502, f"send failed: {e}")

    # ---------- Obsidian vault
    @app.get("/api/obsidian")
    def obsidian_status(p: dict = Depends(principal)):
        require(p, "admin")
        return net.vault.status()

    @app.patch("/api/obsidian")
    def obsidian_prefs(body: VaultPrefs, p: dict = Depends(principal)):
        require(p, "admin")
        prefs = net.vault.set_prefs(body.model_dump(exclude_none=True))
        net.vault.mark_all(s.vault_backfill_days)
        return {"prefs": prefs}

    @app.post("/api/obsidian/sync")
    async def obsidian_sync(p: dict = Depends(principal)):
        require(p, "admin")
        if not net.vault.enabled:
            raise HTTPException(503, "Obsidian sync is off: set VIGILO_VAULT_DIR")
        return await net.vault.sync_now()

    @app.post("/api/obsidian/rebuild")
    async def obsidian_rebuild(days: int = 30, p: dict = Depends(principal)):
        require(p, "admin")
        if not net.vault.enabled:
            raise HTTPException(503, "Obsidian sync is off: set VIGILO_VAULT_DIR")
        queued = net.vault.mark_all(min(max(days, 1), 365))
        st = await net.vault.sync_now()
        return {**st, "queued": queued}

    @app.get("/api/tailscale")
    def tailscale_status(p: dict = Depends(principal)):
        return net.tailscale.summary()

    @app.post("/api/tailscale/sync")
    async def tailscale_sync(p: dict = Depends(principal)):
        require(p, "admin")
        try:
            return await net.tailscale.sync()
        except Exception as e:
            raise HTTPException(502, f"tailscale: {e}")

    @app.get("/api/status")
    def status_json():
        """Public, read-only status data for assets marked public."""
        return {"title": s.status_title, "assets": [
            {"name": a["name"], "kind": a["kind"], "status": json.loads(a["state"] or "{}").get("reported", "unknown"),
             "in_maintenance": (a["maintenance_until"] or 0) > time.time(), "uptime": net.watch.uptime(a["id"]),
             "daily": net.watch.daily(a["id"])} for a in store.all("SELECT * FROM assets WHERE public=1 ORDER BY name")]}

    @app.get("/status", include_in_schema=False)
    def status_page():
        return FileResponse(STATIC_DIR / "status.html")

    # ---------- phone calls (Asterisk ARI)
    @app.post("/api/calls", status_code=201)
    async def place_call(body: CallIn, p: dict = Depends(principal)):
        """Have an agent phone someone. Agents call as themselves; admin picks `agent`."""
        require(p, "admin", "agent")
        if p["kind"] == "agent":
            a = p["agent"]
        else:
            a = net.agent(body.agent or "")
            if not a:
                raise HTTPException(422, "admin calls need `agent`: which agent is calling")
        return await net.tel.place_call(a, body.to, body.message, body.conversation, body.max_turns, body.reason, p["name"])

    @app.get("/api/calls")
    def list_calls(p: dict = Depends(principal)):
        require(p, "admin", "agent")
        calls = [c.public() for c in reversed(net.tel.calls.values())]
        return calls if p["kind"] == "admin" else [c for c in calls if c["agent"] == p["name"]]

    @app.get("/api/calls/{cid}")
    def get_call(cid: str, p: dict = Depends(principal)):
        c = net.tel.calls.get(cid)
        if not c or (p["kind"] != "admin" and c.agent["name"] != p["name"]):
            raise HTTPException(404, "call not found")
        return c.public()

    @app.post("/api/calls/{cid}/hangup")
    async def hangup(cid: str, p: dict = Depends(principal)):
        c = net.tel.calls.get(cid)
        if not c or (p["kind"] != "admin" and c.agent["name"] != p["name"]):
            raise HTTPException(404, "call not found")
        if c.channel and not c.ended:
            try:
                await net.tel.ari("DELETE", f"/channels/{c.channel}")
            except RuntimeError as e:
                raise HTTPException(502, str(e))
        return {"ok": True}

    @app.get("/media/{mid}.wav", include_in_schema=False)
    def media(mid: str):
        """Speech audio for Asterisk to fetch (res_http_media_cache). Unguessable ids, short-lived."""
        data = net.tel.media.get(mid)
        if data is None:
            raise HTTPException(404, "gone")
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # ---------- universal ingest (webhooks, monitors, scripts)
    @app.post("/ingest/{token}")
    async def ingest(token: str, request: Request):
        a = store.one("SELECT * FROM agents WHERE ingest_hash=?", (_hash(token),))
        if not a:
            raise HTTPException(404, "unknown ingest token")
        events = format_webhook(await request.body(), {k.lower(): v for k, v in request.headers.items()},
                                dict(request.query_params))
        posted = []
        for ev in events:
            posted.append(await net.create_post({"kind": "agent", "name": a["name"], "agent": a}, ev.get("channel"),
                                                ev["content"], meta=ev.get("meta"), kind=ev.get("kind", "post")
                                                if ev.get("kind") in POST_KINDS else "post",
                                                severity=ev.get("severity") if ev.get("severity") in SEVERITIES else None))
        return {"ok": True, "posted": [m["id"] for m in posted]}

    @app.post("/ingest/{token}/heartbeat")
    async def ingest_heartbeat(token: str, body: HeartbeatIn):
        a = store.one("SELECT * FROM agents WHERE ingest_hash=?", (_hash(token),))
        if not a:
            raise HTTPException(404, "unknown ingest token")
        return await net.heartbeat(a, body)

    # ---------- v0.1 compatibility
    @app.post("/api/channels/{channel}/messages", status_code=201)
    async def send(channel: str, body: MessageCreate, p: dict = Depends(principal)):
        return await net.create_post(author_of(p), channel, body.content, body.reply_to, body.meta, body.kind, body.severity)

    @app.get("/api/channels/{channel}/messages")
    def history(channel: str, after_id: Optional[int] = None, before_id: Optional[int] = None,
                limit: int = Query(50, ge=1, le=500), p: dict = Depends(principal)):
        ch = store.one("SELECT * FROM channels WHERE name=?", (channel.lower(),))
        if not ch or not net.can_see_channel(p, ch):
            raise HTTPException(404, "channel not found")
        msgs = net.query(p, ["c.id=?"], [ch["id"]], before_id, after_id, limit)
        return msgs if after_id is not None else msgs[::-1]

    @app.get("/api/messages")
    def firehose(after_id: Optional[int] = None, before_id: Optional[int] = None,
                 limit: int = Query(100, ge=1, le=500), p: dict = Depends(principal)):
        msgs = net.query(p, ["c.is_dm=0"], [], before_id, after_id, limit)
        return msgs if after_id is not None else msgs[::-1]

    @app.get("/api/mentions")
    def mentions(after_id: int = 0, limit: int = Query(50, ge=1, le=500), p: dict = Depends(principal)):
        require(p, "agent")
        rows = store.all("SELECT DISTINCT message_id FROM notifications WHERE recipient=? AND type IN ('mention','reply','dm')"
                         " AND message_id>? ORDER BY message_id LIMIT ?", (p["name"], after_id, limit))
        return [m for m in (net.get_message(r["message_id"]) for r in rows) if m]

    # ---------- realtime
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, token: str = Query("")):
        try:
            p = resolve(token)
        except HTTPException:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        hub.clients[websocket] = p
        await websocket.send_text(json.dumps({"type": "hello", "you": {"kind": p["kind"], "name": p["name"]},
                                              "online": hub.online_agents()}))
        if p["kind"] == "agent":
            await hub.broadcast({"type": "presence", "online": hub.online_agents()})
        try:
            while True:
                try:
                    data = json.loads(await websocket.receive_text())
                except json.JSONDecodeError:
                    continue
                t = data.get("type")
                try:
                    if t == "ping":
                        await websocket.send_text('{"type":"pong"}')
                    elif t == "post":
                        await net.create_post(author_of(p), data.get("channel"), data.get("content", ""),
                                              data.get("reply_to"), data.get("meta") or {}, data.get("kind", "post"),
                                              data.get("severity"))
                    elif t == "react":
                        await net.react(author_of(p)["name"], int(data["message_id"]), data["emoji"], data.get("on", True))
                    elif t == "dm":
                        author = author_of(p)
                        ch = net.dm_channel(author["name"], str(data["to"]).lower())
                        await net.create_post(author, ch["name"], data.get("content", ""), data.get("reply_to"))
                except HTTPException as e:
                    await websocket.send_text(json.dumps({"type": "error", "status": e.status_code, "detail": e.detail}))
                except (KeyError, ValueError, TypeError) as e:
                    await websocket.send_text(json.dumps({"type": "error", "status": 400, "detail": str(e)}))
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.pop(websocket, None)
            if p["kind"] == "agent":
                await hub.broadcast({"type": "presence", "online": hub.online_agents()})

    return app


app = create_app() if _env("VIGILO_NO_AUTOAPP") != "1" else None
