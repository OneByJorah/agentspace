"""Read-only watch engine: register assets, probe them, discover new hosts, alert on change.

READ-ONLY BY CONSTRUCTION. Every probe here only *observes*:
  ping   — ICMP echo (system `ping`), falls back to a TCP connect
  tcp    — open a TCP connection and close it
  http   — HTTP GET/HEAD; records status, latency and TLS certificate expiry
  dns    — resolve a name
  snmp   — SNMP v2c **GetRequest** only. There is no SetRequest encoder in this codebase.
Nothing logs in, runs commands, changes config or restarts anything. Findings become posts from the
asset's profile; the analyst worker turns alerts into advice for a human.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from .listeners import _oid, _seq, _tlv, _val, oid_name

log = logging.getLogger("agentchat.watch")

ASSET_KINDS = {
    "server":   {"emoji": "🖥️", "label": "server"},
    "computer": {"emoji": "💻", "label": "computer"},
    "switch":   {"emoji": "🔀", "label": "network device"},
    "website":  {"emoji": "🌐", "label": "website"},
    "software": {"emoji": "📦", "label": "software / service"},
    "camera":   {"emoji": "📷", "label": "camera / IoT"},
    "printer":  {"emoji": "🖨️", "label": "printer"},
}
COMMON_PORTS = [22, 53, 80, 443, 445, 3389, 8080, 8443, 161]
PORT_NAMES = {21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http", 110: "pop3", 135: "msrpc", 139: "netbios",
              143: "imap", 161: "snmp", 389: "ldap", 443: "https", 445: "smb", 515: "lpd", 554: "rtsp", 631: "ipp", 636: "ldaps",
              1433: "mssql", 3306: "mysql", 3389: "rdp", 5060: "sip", 5432: "postgres", 5900: "vnc", 8006: "proxmox",
              8080: "http-alt", 8443: "https-alt", 9100: "jetdirect", 11434: "ollama"}
SNMP_DEFAULT_OIDS = {"sysDescr": "1.3.6.1.2.1.1.1.0", "sysUpTime": "1.3.6.1.2.1.1.3.0", "sysName": "1.3.6.1.2.1.1.5.0"}


# ------------------------------------------------------------------ probes (all read-only)
async def probe_ping(host: str, timeout: float = 2.0) -> dict:
    exe = shutil.which("ping")
    t0 = time.perf_counter()
    if exe:
        p = await asyncio.create_subprocess_exec(exe, "-c", "1", "-W", str(int(max(1, timeout))), host,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        if p.returncode == 0:
            ms = None
            text = out.decode(errors="replace")
            if "time=" in text:
                try:
                    ms = float(text.split("time=")[1].split()[0].replace("ms", ""))
                except ValueError:
                    pass
            return {"ok": True, "ms": ms if ms is not None else round((time.perf_counter() - t0) * 1000, 1)}
        return {"ok": False, "error": "no echo reply"}
    for port in (443, 80, 22):  # no ping binary: any open/refused TCP port proves the host is up
        r = await probe_tcp(host, port, timeout)
        if r["ok"] or "refused" in r.get("error", ""):
            return {"ok": True, "ms": r.get("ms"), "via": f"tcp/{port}"}
    return {"ok": False, "error": "unreachable"}


async def probe_tcp(host: str, port: int, timeout: float = 3.0) -> dict:
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 1)}
    except ConnectionRefusedError:
        return {"ok": False, "error": "connection refused"}
    except (asyncio.TimeoutError, OSError) as e:
        return {"ok": False, "error": "timeout" if isinstance(e, asyncio.TimeoutError) else str(e)[:120]}


def _cert_days(host: str, port: int, timeout: float) -> Optional[int]:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # read expiry even from self-signed certs
    with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=host) as s:
        der = s.getpeercert(binary_form=True)
    try:
        from cryptography import x509  # optional dependency
        cert = x509.load_der_x509_certificate(der)
        end = cert.not_valid_after_utc
    except Exception:
        # fallback: a verified handshake exposes notAfter
        ctx2 = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw, ctx2.wrap_socket(raw, server_hostname=host) as s:
            end = datetime.strptime(s.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).days


async def probe_http(url: str, expect: Optional[int] = None, contains: Optional[str] = None, timeout: float = 10.0) -> dict:
    t0 = time.perf_counter()
    out: dict = {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False,
                                     headers={"User-Agent": "AgentSpace-ReadOnly-Monitor/1.0"}) as c:
            r = await c.get(url)
        out = {"status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        ok = (r.status_code == expect) if expect else r.status_code < 400
        if ok and contains and contains not in r.text:
            ok, out["error"] = False, f"expected text {contains!r} not found"
        out["ok"] = ok
        if not ok and "error" not in out:
            out["error"] = f"HTTP {r.status_code}"
    except Exception as e:
        out = {"ok": False, "error": (type(e).__name__ + ": " + str(e))[:160]}
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        try:
            out["cert_days"] = await asyncio.to_thread(_cert_days, u.hostname, u.port or 443, timeout)
        except Exception:
            pass
    return out


async def probe_dns(name: str) -> dict:
    t0 = time.perf_counter()
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None)
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 1),
                "addresses": sorted({i[4][0] for i in infos})[:6]}
    except socket.gaierror as e:
        return {"ok": False, "error": str(e)[:120]}


def _ber(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        return bytes([tag, n]) + body
    ln = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(ln)]) + ln + body


def _ber_int(n: int) -> bytes:
    return _ber(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def _ber_oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(chunk)
    return _ber(0x06, bytes(out))


READ_ONLY_PDUS = {"get": 0xA0, "getnext": 0xA1}  # GetRequest / GetNextRequest. No SetRequest (0xA3) exists here.


def snmp_get_request(community: str, oids: list[str], request_id: int, op: str = "get") -> bytes:
    """SNMPv2c Get/GetNext request. Read-only by construction: `op` can only be a read PDU."""
    vbs = b"".join(_ber(0x30, _ber_oid(o) + b"\x05\x00") for o in oids)
    pdu = _ber(READ_ONLY_PDUS[op], _ber_int(request_id) + _ber_int(0) + _ber_int(0) + _ber(0x30, vbs))
    return _ber(0x30, _ber_int(1) + _ber(0x04, community.encode()) + pdu)


def parse_get_response(data: bytes) -> dict:
    t, body, _ = _tlv(data, 0)
    items = _seq(body)
    pdu_t, pdu = items[2]
    if pdu_t != 0xA2:
        raise ValueError("not a GetResponse")
    p = _seq(pdu)
    err = _val(*p[1])
    out = {}
    for _, vb in _seq(p[3][1]):
        (_, ov), (vt, vv) = _seq(vb)
        out[_oid(ov)] = _val(vt, vv)
    return {"error_status": err, "values": out}


async def snmp_query(host: str, community: str, oids: list[str], op: str = "get", port: int = 161,
                     timeout: float = 3.0) -> dict:
    """One read-only SNMP exchange over UDP. Returns {oid: value}."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

    tr, _ = await loop.create_datagram_endpoint(P, remote_addr=(host, port))
    try:
        tr.sendto(snmp_get_request(community, oids, int(time.time() * 1000) & 0x7FFFFFFF, op))
        res = parse_get_response(await asyncio.wait_for(fut, timeout))
        if res["error_status"]:
            raise ValueError(f"SNMP error-status {res['error_status']}")
        return res["values"]
    finally:
        tr.close()


async def snmp_walk(host: str, community: str, base: str, max_rows: int = 256, timeout: float = 3.0,
                    port: int = 161) -> dict[str, object]:
    """Walk one table column with GetNext (read-only). Returns {index_suffix: value}."""
    out: dict[str, object] = {}
    cur = base
    for _ in range(max_rows):
        vals = await snmp_query(host, community, [cur], "getnext", port=port, timeout=timeout)
        (oid, val), = vals.items()
        if not oid.startswith(base + ".") or val in ("endOfMibView", "noSuchObject", "noSuchInstance"):
            break
        out[oid[len(base) + 1:]] = val
        cur = oid
    return out


IF_COLS = {"descr": "1.3.6.1.2.1.2.2.1.2", "oper": "1.3.6.1.2.1.2.2.1.8", "name": "1.3.6.1.2.1.31.1.1.1.1",
           "alias": "1.3.6.1.2.1.31.1.1.1.18", "in": "1.3.6.1.2.1.31.1.1.1.6", "out": "1.3.6.1.2.1.31.1.1.1.10",
           "speed": "1.3.6.1.2.1.31.1.1.1.15"}


async def probe_snmp_interfaces(host: str, community: str = "public", prev: Optional[dict] = None,
                                only: Optional[list[str]] = None, timeout: float = 3.0, port: int = 161) -> dict:
    """PRTG-style interface sensor: oper status + in/out bandwidth from 64-bit counters (read-only walk)."""
    try:
        cols = {}
        for k, oid in IF_COLS.items():
            try:
                cols[k] = await snmp_walk(host, community, oid, timeout=timeout, port=port)
            except asyncio.TimeoutError:
                if k in ("descr", "oper"):
                    raise
                cols[k] = {}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "no SNMP response (community/ACL?)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    now = time.time()
    prev_ifs = (prev or {}).get("interfaces", {})
    ifs, down = {}, []
    for idx, descr in cols["descr"].items():
        name = str(cols["name"].get(idx) or descr)
        if only and name not in only and idx not in only:
            continue
        oper = {1: "up", 2: "down", 3: "testing", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}.get(cols["oper"].get(idx), "unknown")
        d = {"name": name, "alias": str(cols["alias"].get(idx) or ""), "oper": oper,
             "speed_mbps": cols["speed"].get(idx), "in": cols["in"].get(idx), "out": cols["out"].get(idx), "ts": now}
        p = prev_ifs.get(idx)
        if p and d["in"] is not None and p.get("in") is not None and now > p["ts"]:
            dt = now - p["ts"]
            d["in_bps"] = max(0, (d["in"] - p["in"]) * 8 / dt) if d["in"] >= p["in"] else None
            d["out_bps"] = max(0, (d["out"] - p["out"]) * 8 / dt) if d["out"] >= p["out"] else None
            if d["speed_mbps"]:
                d["util_pct"] = round(100 * max(d.get("in_bps") or 0, d.get("out_bps") or 0) / (d["speed_mbps"] * 1e6), 1)
        # a port counts as "in service" once it has been seen up; it stays failed until it comes back up,
        # so a persistent outage keeps the check red (unused ports that were never up stay quiet)
        d["in_service"] = oper == "up" or bool(p and (p.get("in_service") or p.get("oper") == "up"))
        ifs[idx] = d
        if d["in_service"] and oper != "up":
            down.append(f"{name} ({d['alias']})" if d["alias"] else name)
    res = {"ok": not down, "interfaces": ifs, "up": sum(1 for d in ifs.values() if d["oper"] == "up"), "total": len(ifs)}
    if down:
        res["error"] = "interface down: " + ", ".join(down[:8])
    return res


async def probe_snmp(host: str, community: str = "public", oids: Optional[list[str]] = None, port: int = 161,
                     timeout: float = 3.0) -> dict:
    oids = oids or list(SNMP_DEFAULT_OIDS.values())
    loop = asyncio.get_running_loop()
    rid = int(time.time() * 1000) & 0x7FFFFFFF
    fut: asyncio.Future = loop.create_future()

    class P(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            if not fut.done():
                fut.set_result(data)

        def error_received(self, exc):
            if not fut.done():
                fut.set_exception(exc)

    t0 = time.perf_counter()
    tr, _ = await loop.create_datagram_endpoint(P, remote_addr=(host, port))
    try:
        tr.sendto(snmp_get_request(community, oids, rid))
        data = await asyncio.wait_for(fut, timeout)
        res = parse_get_response(data)
        vals = {oid_name(k): (str(v)[:200] if v is not None else None) for k, v in res["values"].items()}
        return {"ok": res["error_status"] == 0, "ms": round((time.perf_counter() - t0) * 1000, 1), "values": vals}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "no SNMP response (community/ACL?)"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    finally:
        tr.close()


async def run_check(c: dict, target: str, prev: Optional[dict] = None, agent: Optional[dict] = None) -> dict:
    t = c.get("type")
    host = urlparse(target).hostname if "://" in target else target.split(":")[0]
    if t == "ping":
        return await probe_ping(c.get("host") or host)
    if t == "tcp":
        return await probe_tcp(c.get("host") or host, int(c["port"]))
    if t == "http":
        return await probe_http(c.get("url") or target, c.get("expect"), c.get("contains"))
    if t == "dns":
        return await probe_dns(c.get("name") or host)
    if t == "snmp":
        return await probe_snmp(c.get("host") or host, c.get("community", "public"), c.get("oids"), int(c.get("port", 161)))
    if t == "snmp_if":
        return await probe_snmp_interfaces(c.get("host") or host, c.get("community", "public"), prev, c.get("interfaces"),
                                           port=int(c.get("port", 161)))
    if t == "tailscale":
        from .tailscale import NODES
        n = NODES.get(str(c.get("node")))
        if not n:
            return {"ok": False, "error": "node no longer in the tailnet"}
        if n.get("_self") or n.get("Online"):
            return {"ok": True}
        return {"ok": False, "error": f"offline on Tailscale (last seen {str(n.get('LastSeen', '?'))[:16].replace('T', ' ')})"}
    if t == "push":
        last = (agent or {}).get("last_heartbeat") or 0
        age = time.time() - last
        grace = int(c.get("grace_s", 600))
        return {"ok": age <= grace, "age_s": int(age) if last else None,
                **({} if age <= grace else {"error": "no heartbeat yet" if not last else f"silent for {int(age // 60)} min (grace {grace // 60} min)"})}
    return {"ok": False, "error": f"unknown check type {t!r}"}


def check_label(c: dict) -> str:
    t = c.get("type")
    if t == "tcp":
        return f"tcp/{c['port']} ({PORT_NAMES.get(int(c['port']), 'port')})"
    if t == "http":
        return f"http {c.get('url', '')}".strip()
    if t == "snmp_if":
        return "snmp interfaces"
    if t == "push":
        return "heartbeat"
    if t == "tailscale":
        return "tailscale online"
    return t or "?"


def default_checks(kind: str, target: str, ports: Optional[list[int]] = None, community: Optional[str] = None) -> list[dict]:
    if "://" in target:
        checks = [{"type": "http", "url": target}]
        if kind != "website":
            checks.insert(0, {"type": "ping"})
        return checks
    has_port = ":" in target and target.rsplit(":", 1)[1].isdigit()
    # for a service (host:port) the port IS the service: a host that still pings shouldn't hide an outage
    checks: list[dict] = [{"type": "ping", "optional": True} if has_port or kind == "software" else {"type": "ping"}]
    if has_port:
        checks.append({"type": "tcp", "port": int(target.rsplit(":", 1)[1])})
    for p in ports or []:
        checks.append({"type": "tcp", "port": int(p)})
    if kind == "switch" or community:
        checks.append({"type": "snmp", "community": community or "public"})
        if kind == "switch":
            checks.append({"type": "snmp_if", "community": community or "public"})
    return checks


# ------------------------------------------------------------------ engine
class Watch:
    def __init__(self, net) -> None:
        self.net, self.s = net, net.s
        self.sem = asyncio.Semaphore(self.s.watch_concurrency)
        self.running: set[int] = set()
        self.last_discovery = 0.0
        self.discovering = False
        self.extra_cidrs: list[str] = []  # e.g. Tailscale subnet routes

    def asset(self, aid: int) -> Optional[dict]:
        return self.net.store.one("SELECT * FROM assets WHERE id=?", (aid,))

    def uptime(self, aid: int) -> dict:
        now, out = time.time(), {}
        for label, secs in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
            r = self.net.store.one("SELECT AVG(ok) AS u, COUNT(*) AS n, AVG(ms) AS ms FROM samples WHERE asset_id=? AND ts>?",
                                   (aid, now - secs))
            out[label] = round(r["u"] * 100, 2) if r and r["n"] else None
            if label == "24h":
                out["avg_ms_24h"] = round(r["ms"], 1) if r and r["ms"] is not None else None
        return out

    def history(self, aid: int, hours: int = 24, points: int = 60) -> list[dict]:
        since = time.time() - hours * 3600
        bucket = max(60, hours * 3600 // points)
        return self.net.store.all(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, AVG(ok) AS up, AVG(ms) AS ms FROM samples "
            "WHERE asset_id=? AND ts>? GROUP BY CAST(ts / ? AS INTEGER) ORDER BY t", (bucket, bucket, aid, since, bucket))

    def daily(self, aid: int, days: int = 30) -> list[dict]:
        return self.net.store.all(
            "SELECT date(ts, 'unixepoch') AS day, AVG(ok) AS up FROM samples WHERE asset_id=? AND ts>? GROUP BY day ORDER BY day",
            (aid, time.time() - days * 86400))

    def public(self, a: dict, stats: bool = True) -> dict:
        d = dict(a)
        d["checks"], d["state"] = json.loads(a["checks"]), json.loads(a["state"] or "{}")
        d["enabled"], d["critical"], d["public"] = bool(a["enabled"]), bool(a["critical"]), bool(a["public"])
        d["in_maintenance"] = (a["maintenance_until"] or 0) > time.time()
        if stats:
            d["uptime"] = self.uptime(a["id"])
        return d

    async def run(self) -> None:
        last_prune = 0.0
        while True:
            try:
                now = time.time()
                if now - last_prune > 3600:
                    last_prune = now
                    self.net.store.run("DELETE FROM samples WHERE ts<?", (now - self.s.history_days * 86400,))
                for a in self.net.store.all("SELECT * FROM assets WHERE enabled=1"):
                    if a["id"] not in self.running and now - (a["last_check"] or 0) >= a["interval_s"]:
                        self.running.add(a["id"])
                        self.net.spawn(self.check_asset(a["id"]))
                cidrs = sorted(set(self.s.discovery_cidrs) | set(self.extra_cidrs))
                if cidrs and not self.discovering and now - self.last_discovery > self.s.discovery_interval_min * 60:
                    self.last_discovery = now
                    self.net.spawn(self.discover(cidrs, None, "scheduled"))
            except Exception:
                log.exception("watch loop error")
            await asyncio.sleep(5)

    async def check_asset(self, aid: int) -> Optional[dict]:
        try:
            a = self.asset(aid)
            if not a:
                return None
            checks = json.loads(a["checks"])
            prev = json.loads(a["state"] or "{}")
            agent = self.net.agent(a["agent_name"])
            async with self.sem:
                results = await asyncio.gather(*(run_check(c, a["target"], {"interfaces": prev.get("interfaces", {})}, agent)
                                                 for c in checks), return_exceptions=True)
            results = [r if isinstance(r, dict) else {"ok": False, "error": str(r)[:120]} for r in results]
            return await self.evaluate(a, checks, results)
        finally:
            self.running.discard(aid)

    async def evaluate(self, a: dict, checks: list[dict], results: list[dict]) -> dict:
        net = self.net
        prev = json.loads(a["state"] or "{}")
        for c, r in zip(checks, results):  # latency threshold → degraded, not down
            if r.get("ok") and c.get("warn_ms") and (r.get("ms") or 0) > float(c["warn_ms"]):
                r["ok"], r["error"], r["slow"] = False, f"slow: {r['ms']} ms > {c['warn_ms']} ms", True
            if r.get("ok") and c.get("type") == "snmp_if":
                hot = [d["name"] for d in r["interfaces"].values() if (d.get("util_pct") or 0) >= float(c.get("bw_warn_pct", 80))]
                if hot:
                    r["ok"], r["error"], r["slow"] = False, f"bandwidth ≥{c.get('bw_warn_pct', 80)}%: " + ", ".join(hot[:6]), True
        for c, r in zip(checks, results):  # optional checks (e.g. ping to a host that drops ICMP) never raise status
            if c.get("optional") and not r.get("ok"):
                r["ok"], r["note"] = True, "optional: " + r.pop("error", "failed")
        failing = [check_label(c) + ": " + r.get("error", "failed") for c, r in zip(checks, results) if not r.get("ok")]
        cert_days = next((r["cert_days"] for r in results if r.get("cert_days") is not None), None)
        latency = [r["ms"] for r in results if r.get("ok") and r.get("ms") is not None]
        hard = [r for r in results if not r.get("ok") and not r.get("slow")]
        if not failing:
            status = "up"
        elif len(hard) >= len([c for c in checks if not c.get("optional")]):
            status = "down"
        else:
            status = "degraded"
        fails = prev.get("fail_streak", 0) + 1 if status != "up" else 0
        # hysteresis: only declare a problem after 2 consecutive bad rounds; recover immediately
        reported = prev.get("reported", "unknown")
        new_reported = status if (status == "up" or fails >= 2) else reported
        state = {"status": status, "reported": new_reported, "fail_streak": fails, "failing": failing,
                 "latency_ms": round(sum(latency) / len(latency), 1) if latency else None, "cert_days": cert_days,
                 "results": [{"check": check_label(c), **{k: v for k, v in r.items() if k in ("ok", "ms", "error", "status", "cert_days")}}
                             for c, r in zip(checks, results)],
                 "snmp": next((r["values"] for r in results if r.get("values")), prev.get("snmp")),
                 "interfaces": next((r["interfaces"] for r in results if r.get("interfaces")), prev.get("interfaces", {})),
                 "since": prev.get("since") if new_reported == reported else time.time(),
                 "cert_alerted": prev.get("cert_alerted")}
        net.store.run("UPDATE assets SET state=?, last_check=? WHERE id=?", (json.dumps(state), time.time(), a["id"]))
        net.store.run("INSERT INTO samples(asset_id, ts, ok, ms) VALUES (?,?,?,?)",
                      (a["id"], time.time(), int(status != "down"), state["latency_ms"]))
        in_maintenance = (a["maintenance_until"] or 0) > time.time()
        agent = net.agent(a["agent_name"])
        if agent:
            metrics = {"status": new_reported}
            if state["latency_ms"] is not None:
                metrics["latency_ms"] = state["latency_ms"]
            if cert_days is not None:
                metrics["cert_days"] = cert_days
            if any(c.get("type") == "push" for c in checks):  # its heartbeat comes from the job itself
                net.store.run("UPDATE agents SET status=? WHERE id=?", (new_reported, agent["id"]))
            else:
                net.store.run("UPDATE agents SET heartbeat=?, last_heartbeat=?, status=? WHERE id=?",
                              (json.dumps(metrics), time.time() if new_reported != "down" else agent["last_heartbeat"],
                               new_reported, agent["id"]))
            author = {"kind": "agent", "name": agent["name"], "agent": agent}
            changed = (new_reported != reported and reported != "unknown") or (reported == "unknown" and new_reported in ("down", "degraded"))
            if changed and in_maintenance:
                log.info("%s changed to %s during maintenance — alert muted", a["name"], new_reported)
            elif changed:
                if new_reported == "up":
                    down_for = int(time.time() - (prev.get("since") or time.time()))
                    text = f"🟢 **{a['name']}** is back UP" + (f" after {down_for // 60}m {down_for % 60}s" if down_for else "") \
                        + (f" · {state['latency_ms']} ms" if state["latency_ms"] else "")
                    sev = "ok"
                else:
                    icon = "🔴" if new_reported == "down" else "🟠"
                    text = f"{icon} **{a['name']}** is {new_reported.upper()} ({a['target']})\n" + "\n".join(f"• {f}" for f in failing[:6])
                    sev = "error" if new_reported == "down" else "warning"
                    if new_reported == "down" and a["critical"]:
                        sev = "critical"
                await self._post(author, text, sev, {"asset": a["id"], "metrics": metrics})
            if cert_days is not None and not in_maintenance:
                level = "error" if cert_days <= 3 else "warning" if cert_days <= self.s.cert_warn_days else None
                if level and prev.get("cert_alerted") != level:
                    state["cert_alerted"] = level
                    net.store.run("UPDATE assets SET state=? WHERE id=?", (json.dumps(state), a["id"]))
                    await self._post(author, f"🔐 TLS certificate for **{a['name']}** expires in **{cert_days} day(s)**.",
                                     level, {"asset": a["id"], "metrics": {"cert_days": cert_days}})
        await net.hub.broadcast({"type": "asset", "asset": self.public(self.asset(a["id"]))})
        if (new_reported != reported) or not prev.get("vault_synced") or time.time() - prev.get("vault_synced", 0) > 900:
            net.vault.on_asset(a["id"])  # status changes immediately, otherwise refresh the note every 15 min
            state["vault_synced"] = time.time()
            net.store.run("UPDATE assets SET state=? WHERE id=?", (json.dumps(state), a["id"]))
        return state

    async def _post(self, author: dict, text: str, severity: str, meta: dict) -> None:
        from fastapi import HTTPException
        try:
            await self.net.create_post(author, "alerts", text, kind="alert" if severity != "ok" else "status",
                                       severity=severity, meta=meta, enforce_rate=False)
        except HTTPException as e:
            log.warning("watch post failed: %s", e.detail)

    # ---------------------------------------------------------- discovery (read-only sweep)
    async def discover(self, cidrs: list[str], ports: Optional[list[int]], by: str) -> dict:
        net = self.net
        if self.discovering:
            return {"started": False, "reason": "a discovery is already running"}
        self.discovering = True
        ports = ports or COMMON_PORTS
        try:
            hosts: list[str] = []
            for cidr in cidrs:
                n = ipaddress.ip_network(cidr.strip(), strict=False)
                if n.num_addresses > 4096:
                    raise ValueError(f"{cidr} is too large (max /20)")
                hosts += [str(h) for h in (n.hosts() if n.num_addresses > 2 else [n.network_address])]
            sem = asyncio.Semaphore(self.s.discovery_concurrency)
            known_ips = {r["ip"] for r in net.store.all("SELECT ip FROM discoveries")}
            registered = {json.loads(a["checks"])[0].get("host") or a["target"] for a in net.store.all("SELECT * FROM assets")}

            async def scan(ip: str) -> Optional[dict]:
                async with sem:
                    open_ports = []
                    res = await asyncio.gather(*(probe_tcp(ip, p, 1.0) for p in ports if p != 161))
                    for p, r in zip([p for p in ports if p != 161], res):
                        if r["ok"]:
                            open_ports.append(p)
                    alive = bool(open_ports) or any("refused" in r.get("error", "") for r in res)
                    if not alive:
                        alive = (await probe_ping(ip, 1))["ok"]
                    if not alive:
                        return None
                    try:
                        name = (await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, ip), 2))[0]
                    except Exception:
                        name = None
                    snmp = None
                    if 161 in ports:
                        r = await probe_snmp(ip, self.s.discovery_snmp_community, timeout=1.0)
                        snmp = r.get("values") if r["ok"] else None
                    return {"ip": ip, "hostname": name, "ports": open_ports, "snmp": snmp}

            found = [r for r in await asyncio.gather(*(scan(h) for h in hosts)) if r]
            new = []
            for f in found:
                is_new = f["ip"] not in known_ips
                net.store.run("INSERT INTO discoveries(ip, hostname, ports, snmp, first_seen, last_seen) VALUES (?,?,?,?,?,?) "
                              "ON CONFLICT(ip) DO UPDATE SET hostname=excluded.hostname, ports=excluded.ports, "
                              "snmp=COALESCE(excluded.snmp, snmp), last_seen=excluded.last_seen",
                              (f["ip"], f["hostname"], json.dumps(f["ports"]), json.dumps(f["snmp"]) if f["snmp"] else None,
                               time.time(), time.time()))
                if is_new and f["ip"] not in registered:
                    new.append(f)
            scout = net.agent(self.s.discovery_agent) or None
            author = {"kind": "agent", "name": scout["name"], "agent": scout} if scout else {"kind": "system", "name": "agentchat"}

            def fmt(f):
                svc = ", ".join(PORT_NAMES.get(p, str(p)) for p in f["ports"]) or "no common ports"
                sys = (f["snmp"] or {}).get("sysDescr")
                return f"• `{f['ip']}`" + (f" {f['hostname']}" if f["hostname"] else "") + f" — {svc}" + (f" — {sys[:60]}" if sys else "")
            first_run = not known_ips
            if new and not first_run:
                text = (f"🔎 **{len(new)} new device(s)** appeared on {', '.join(cidrs)}:\n" + "\n".join(fmt(f) for f in new[:15])
                        + ("\n…" if len(new) > 15 else "") + "\nUnknown devices should be identified. Register them on the Assets page.")
                await self._post(author, text, "warning", {"discovery": True})
            else:
                text = (f"🔎 Discovery of {', '.join(cidrs)}: **{len(found)} host(s)** up"
                        + (" (baseline recorded)" if first_run else ", nothing new") + ".\n" + "\n".join(fmt(f) for f in found[:10]))
                try:
                    await net.create_post(author, "network", text, kind="status", meta={"discovery": True}, enforce_rate=False)
                except Exception:
                    pass
            await net.hub.broadcast({"type": "discovery", "found": len(found), "new": len(new)})
            return {"started": True, "found": len(found), "new": len(new), "hosts": found}
        finally:
            self.discovering = False
