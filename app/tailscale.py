"""Tailscale auto-discovery (read-only).

Reads the local tailscaled status (same data as `tailscale status --json`) through its unix socket — or the
`tailscale` CLI if the socket isn't mounted — and:
  • registers every tailnet node as a watched asset (online/offline from the coordination server + latency),
  • collects every advertised subnet route so discovery sweeps those LANs too,
  • alerts when a NEW node joins the tailnet (a security-relevant event).
Only GET /localapi/v0/status is ever called. Nothing on the tailnet is changed.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import time
from typing import Optional

import httpx

log = logging.getLogger("agentchat.tailscale")

NODES: dict[str, dict] = {}  # node id → latest peer record (read by the "tailscale" check in watch.py)


def _kind(os_name: str) -> str:
    os_name = (os_name or "").lower()
    if os_name in ("linux", "freebsd", "openbsd"):
        return "server"
    if os_name in ("ios", "android"):
        return "computer"
    return "computer"


def _handle(host: str) -> str:
    h = re.sub(r"[^a-z0-9_-]+", "-", (host or "node").lower()).strip("-")[:28] or "node"
    return h


class TailscaleSync:
    def __init__(self, net) -> None:
        self.net, self.s = net, net.s
        self.last: Optional[dict] = None
        self.routes: list[str] = []
        self.skipped_routes: list[str] = []
        self.error: Optional[str] = None
        self.synced_at: Optional[float] = None

    @property
    def available(self) -> bool:
        return os.path.exists(self.s.tailscale_socket) or bool(shutil.which("tailscale"))

    async def status(self) -> dict:
        if os.path.exists(self.s.tailscale_socket):
            transport = httpx.AsyncHTTPTransport(uds=self.s.tailscale_socket)
            async with httpx.AsyncClient(transport=transport, timeout=10) as c:
                r = await c.get("http://local-tailscaled.sock/localapi/v0/status",
                                headers={"Host": "local-tailscaled.sock"})
                r.raise_for_status()
                return r.json()
        exe = shutil.which("tailscale")
        if not exe:
            raise RuntimeError(f"no tailscaled socket at {self.s.tailscale_socket} and no tailscale CLI")
        p = await asyncio.create_subprocess_exec(exe, "status", "--json", stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        out, err = await p.communicate()
        if p.returncode:
            raise RuntimeError(err.decode()[:200])
        return json.loads(out)

    def summary(self) -> dict:
        nodes = list(NODES.values())
        return {"available": self.available, "error": self.error, "synced_at": self.synced_at,
                "tailnet": (self.last or {}).get("CurrentTailnet", {}).get("Name") if self.last else None,
                "nodes": [{"id": n.get("ID"), "host": n.get("HostName"), "dns": n.get("DNSName", "").rstrip("."),
                           "ip": next((i for i in n.get("TailscaleIPs", []) if ":" not in i), None), "os": n.get("OS"),
                           "online": bool(n.get("Online")), "self": bool(n.get("_self")), "last_seen": n.get("LastSeen"),
                           "routes": [r for r in (n.get("PrimaryRoutes") or []) if r not in ("0.0.0.0/0", "::/0")],
                           "exit_node_option": bool(n.get("ExitNodeOption"))} for n in nodes],
                "routes": self.routes, "skipped_routes": self.skipped_routes}

    async def run(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception as e:
                self.error = str(e)[:200]
                log.debug("tailscale sync failed: %s", e)
            await asyncio.sleep(self.s.tailscale_interval_min * 60)

    async def sync(self) -> dict:
        st = await self.status()
        self.last, self.error, self.synced_at = st, None, time.time()
        first = not NODES
        me = dict(st.get("Self") or {}, _self=True)
        peers = [me] + list((st.get("Peer") or {}).values())
        seen_before = set(NODES)
        NODES.clear()
        routes, skipped, new_nodes = set(), set(), []
        for n in peers:
            nid = str(n.get("ID") or n.get("PublicKey"))
            NODES[nid] = n
            for r in n.get("PrimaryRoutes") or []:
                if r in ("0.0.0.0/0", "::/0") or ":" in r:
                    continue
                (routes if ipaddress.ip_network(r, strict=False).num_addresses <= 4096 else skipped).add(r)
            if nid not in seen_before and not first:
                new_nodes.append(n)
            if self.s.tailscale_autoregister:
                await self._register(nid, n)
        self.routes, self.skipped_routes = sorted(routes), sorted(skipped)
        if self.s.tailscale_sweep_routes:
            self.net.watch.extra_cidrs = self.routes
        author = self._author()
        if first:
            online = sum(1 for n in peers if n.get("Online") or n.get("_self"))
            text = (f"🧭 Tailscale connected: **{len(peers)} node(s)**, {online} online"
                    + (f", subnet routes {', '.join(self.routes)}" if self.routes else "")
                    + (f" (too large to sweep: {', '.join(self.skipped_routes)})" if self.skipped_routes else "")
                    + ". Every node is now watched read-only.")
            await self._post(author, text, "status", None)
        for n in new_nodes:
            await self._post(author, f"🆕 New node joined the tailnet: **{n.get('HostName')}** ({n.get('OS')}, "
                             f"{', '.join(n.get('TailscaleIPs', [])[:1])}). Confirm it's expected.", "alert", "warning")
        await self.net.hub.broadcast({"type": "tailscale", "summary": {"nodes": len(peers), "routes": self.routes}})
        return self.summary()

    def _author(self) -> dict:
        a = self.net.agent(self.s.discovery_agent)
        return {"kind": "agent", "name": a["name"], "agent": a} if a else {"kind": "system", "name": "agentchat"}

    async def _post(self, author: dict, text: str, kind: str, severity: Optional[str]) -> None:
        try:
            await self.net.create_post(author, "network", text, kind=kind, severity=severity,
                                       meta={"source": "tailscale"}, enforce_rate=False)
        except Exception as e:
            log.warning("tailscale post failed: %s", e)

    async def _register(self, nid: str, n: dict) -> None:
        store = self.net.store
        ip = next((i for i in n.get("TailscaleIPs", []) if ":" not in i), None)
        if not ip:
            return
        if store.one("SELECT 1 FROM assets WHERE checks LIKE ?", (f'%"node": "{nid}"%',)):
            return
        base = _handle(n.get("HostName"))
        name = base if not self.net.agent(base) else f"{base}-ts"
        if self.net.agent(name) and store.one("SELECT 1 FROM assets WHERE agent_name=?", (name,)):
            return
        from .main import OnboardIn  # local import: avoid a cycle at module load
        kind = _kind(n.get("OS"))
        if not self.net.agent(name):
            await self.net.onboard(OnboardIn(name=name, kind=kind, display_name=n.get("HostName"),
                                             description=f"Tailscale node {n.get('DNSName', '').rstrip('.')} ({n.get('OS')}) at {ip}, "
                                                         "discovered automatically."),
                                   self.s.public_url or "http://HUB", created_by="tailscale")
        checks = [{"type": "tailscale", "node": nid}]
        if not n.get("_self"):
            checks.append({"type": "ping", "host": ip, "optional": True})
        store.run("INSERT INTO assets(agent_name, name, kind, target, checks, interval_s, created_at) VALUES (?,?,?,?,?,?,?)",
                  (name, n.get("HostName") or name, kind, ip, json.dumps(checks), 120, time.time()))
