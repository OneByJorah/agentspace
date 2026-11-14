"""Turn arbitrary webhook payloads into feed posts.

Recognizes Prometheus Alertmanager, Grafana alerting, Uptime Kuma, GitHub, Slack/Discord-style
messages, plain text, and falls back to pretty-printed JSON for anything else.
"""
from __future__ import annotations

import json
from typing import Any

SEV_MAP = {"critical": "critical", "crit": "critical", "page": "critical", "high": "error", "error": "error",
           "err": "error", "major": "error", "warning": "warning", "warn": "warning", "minor": "warning",
           "medium": "warning", "info": "info", "low": "info", "none": "info", "ok": "ok", "resolved": "ok"}
TEXT_KEYS = ("content", "text", "message", "msg", "body", "description", "summary")


def _ev(content: str, kind: str = "post", severity: str | None = None, meta: dict | None = None) -> dict:
    return {"content": content.strip()[:4000] or "(empty payload)", "kind": kind, "severity": severity, "meta": meta or {}}


def _alertmanager(data: dict) -> list[dict]:
    out = []
    for a in data["alerts"][:10]:
        labels, ann = a.get("labels", {}) or {}, a.get("annotations", {}) or {}
        status = (a.get("status") or data.get("status") or "firing").lower()
        name = labels.get("alertname") or data.get("title") or "Alert"
        where = labels.get("instance") or labels.get("job") or labels.get("host") or ""
        detail = ann.get("summary") or ann.get("description") or a.get("valueString") or ""
        sev = "ok" if status == "resolved" else SEV_MAP.get(str(labels.get("severity", "warning")).lower(), "warning")
        icon = "✅" if status == "resolved" else {"critical": "🔥", "error": "🔴"}.get(sev, "⚠️")
        text = f"{icon} **{name}** {status.upper()}" + (f" on {where}" if where else "") + (f"\n{detail}" if detail else "")
        out.append(_ev(text, "alert", sev, {"source": "alertmanager", "labels": labels}))
    return out or [_ev(data.get("title") or "Empty alert batch", "alert", "info")]


def _uptime_kuma(data: dict) -> dict:
    up = (data.get("heartbeat") or {}).get("status") == 1
    mon = (data.get("monitor") or {}).get("name") or "monitor"
    msg = data.get("msg") or (data.get("heartbeat") or {}).get("msg") or ""
    text = f"{'🟢' if up else '🔴'} **{mon}** is {'UP' if up else 'DOWN'}" + (f" — {msg}" if msg else "")
    return _ev(text, "alert", "ok" if up else "error", {"source": "uptime-kuma"})


def _github(event: str, d: dict) -> dict:
    repo = (d.get("repository") or {}).get("full_name", "repo")
    sender = (d.get("sender") or {}).get("login", "someone")
    if event == "push":
        commits = d.get("commits") or []
        head = ((d.get("head_commit") or {}).get("message") or "").splitlines()[:1]
        return _ev(f"⬆️ {sender} pushed {len(commits)} commit(s) to {repo}@{d.get('ref', '').split('/')[-1]}"
                   + (f": {head[0]}" if head else ""), meta={"source": "github", "event": event})
    if event in ("pull_request", "issues"):
        obj = d.get("pull_request") or d.get("issue") or {}
        icon = "🔀" if event == "pull_request" else "🐛"
        return _ev(f"{icon} #{obj.get('number')} {d.get('action')} by {sender}: {obj.get('title')} ({repo})\n{obj.get('html_url', '')}",
                   meta={"source": "github", "event": event})
    if event == "workflow_run":
        run = d.get("workflow_run") or {}
        concl = run.get("conclusion") or run.get("status")
        sev = {"success": "ok", "failure": "error", "cancelled": "warning"}.get(concl, "info")
        return _ev(f"{'✅' if sev == 'ok' else '❌' if sev == 'error' else '⚙️'} {run.get('name')} {concl} on {repo}",
                   "alert" if sev == "error" else "status", sev, {"source": "github", "event": event})
    if event == "release":
        rel = d.get("release") or {}
        return _ev(f"🚀 {repo} released {rel.get('tag_name')}: {rel.get('name') or ''}", meta={"source": "github"})
    if event == "star":
        return _ev(f"⭐ {sender} starred {repo}", meta={"source": "github"})
    return _ev(f"🐙 GitHub `{event}` on {repo} by {sender}", meta={"source": "github", "event": event})


def format_webhook(body: bytes, headers: dict[str, str], query: dict[str, str]) -> list[dict]:
    text = body.decode("utf-8", "replace").strip()
    data: Any = None
    if text[:1] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None

    if data is None:
        events = [_ev(text)]
    elif headers.get("x-github-event"):
        events = [_github(headers["x-github-event"], data if isinstance(data, dict) else {})]
    elif isinstance(data, dict) and isinstance(data.get("alerts"), list):
        events = _alertmanager(data)
    elif isinstance(data, dict) and "heartbeat" in data and "monitor" in data:
        events = [_uptime_kuma(data)]
    elif isinstance(data, dict) and any(isinstance(data.get(k), str) and data[k].strip() for k in TEXT_KEYS):
        content = next(data[k] for k in TEXT_KEYS if isinstance(data.get(k), str) and data[k].strip())
        title = data.get("title")
        sev = SEV_MAP.get(str(data.get("severity") or data.get("level") or "").lower())
        events = [_ev((f"**{title}**\n" if isinstance(title, str) and title else "") + content,
                      "alert" if sev in ("critical", "error", "warning") else "post", sev,
                      {k: v for k, v in data.items() if k in ("metrics", "tags", "source")})]
    elif isinstance(data, dict) and isinstance(data.get("embeds"), list) and data["embeds"]:
        e = data["embeds"][0]
        events = [_ev(f"**{e.get('title', '')}**\n{e.get('description', '')}")]
    else:
        pretty = json.dumps(data, indent=2)[:1500]
        events = [_ev(f"📦 Payload received\n```json\n{pretty}\n```", meta={"raw": True})]

    for ev in events:
        if query.get("kind"):
            ev["kind"] = query["kind"]
        if query.get("severity"):
            ev["severity"] = SEV_MAP.get(query["severity"].lower(), query["severity"])
        if query.get("channel"):
            ev["channel"] = query["channel"]
    return events
