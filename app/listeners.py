"""UDP listeners: SNMP v1/v2c traps and syslog (RFC 3164 / 5424).

No external SNMP library: a small BER decoder handles the trap PDUs. Each packet is mapped to an
agent by source IP; unknown devices can be auto-onboarded with their own profile.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger("agentchat.listeners")

# ---------------------------------------------------------------- SNMP
TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
UPTIME_OID = "1.3.6.1.2.1.1.3.0"
GENERIC_V1 = ["1.3.6.1.6.3.1.1.5.1", "1.3.6.1.6.3.1.1.5.2", "1.3.6.1.6.3.1.1.5.3",
              "1.3.6.1.6.3.1.1.5.4", "1.3.6.1.6.3.1.1.5.5", "1.3.6.1.6.3.1.1.5.6"]
TRAPS = {  # oid: (label, emoji, severity)
    "1.3.6.1.6.3.1.1.5.1": ("Cold start (rebooted)", "🔄", "warning"),
    "1.3.6.1.6.3.1.1.5.2": ("Warm start", "🔄", "info"),
    "1.3.6.1.6.3.1.1.5.3": ("Link DOWN", "🔻", "error"),
    "1.3.6.1.6.3.1.1.5.4": ("Link UP", "🔺", "ok"),
    "1.3.6.1.6.3.1.1.5.5": ("Authentication failure", "🚨", "warning"),
    "1.3.6.1.6.3.1.1.5.6": ("EGP neighbor loss", "⚠️", "warning"),
}
OID_NAMES = {
    "1.3.6.1.2.1.1.3.0": "sysUpTime", "1.3.6.1.2.1.1.5.0": "sysName", TRAP_OID: "snmpTrapOID",
    "1.3.6.1.2.1.2.2.1.1": "ifIndex", "1.3.6.1.2.1.2.2.1.2": "ifDescr", "1.3.6.1.2.1.2.2.1.7": "ifAdminStatus",
    "1.3.6.1.2.1.2.2.1.8": "ifOperStatus", "1.3.6.1.2.1.31.1.1.1.1": "ifName", "1.3.6.1.2.1.31.1.1.1.18": "ifAlias",
}


def oid_name(oid: str) -> str:
    for base, label in OID_NAMES.items():
        if oid == base:
            return label
        if oid.startswith(base + "."):
            return f"{label}.{oid[len(base) + 1:]}"
    return oid


def _tlv(b: bytes, i: int) -> tuple[int, bytes, int]:
    tag = b[i]
    ln = b[i + 1]
    j = i + 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(b[j:j + n], "big")
        j += n
    if j + ln > len(b):
        raise ValueError("truncated BER")
    return tag, b[j:j + ln], j + ln


def _seq(b: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(b):
        t, v, i = _tlv(b, i)
        out.append((t, v))
    return out


def _oid(v: bytes) -> str:
    parts, n = [v[0] // 40, v[0] % 40], 0
    for byte in v[1:]:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def _val(t: int, v: bytes) -> Any:
    if t == 0x02:
        return int.from_bytes(v, "big", signed=True)
    if t == 0x04:
        try:
            s = v.decode("utf-8")
            return s if s.isprintable() else v.hex(":")
        except UnicodeDecodeError:
            return v.hex(":")
    if t == 0x05:
        return None
    if t == 0x06:
        return _oid(v)
    if t == 0x40:
        return ".".join(str(x) for x in v)
    if t in (0x41, 0x42, 0x43, 0x46, 0x47):
        return int.from_bytes(v, "big")
    if t in (0x80, 0x81, 0x82):
        return ["noSuchObject", "noSuchInstance", "endOfMibView"][t - 0x80]
    return v.hex()


def parse_trap(data: bytes) -> dict:
    t, body, _ = _tlv(data, 0)
    if t != 0x30:
        raise ValueError("not an SNMP message")
    items = _seq(body)
    version = _val(*items[0])
    if version not in (0, 1):
        raise ValueError(f"unsupported SNMP version {version} (v3: forward via snmptrapd)")
    community = items[1][1].decode("utf-8", "replace")
    pdu_t, pdu = items[2]
    p = _seq(pdu)
    out: dict[str, Any] = {"version": "v1" if version == 0 else "v2c", "community": community,
                           "inform": pdu_t == 0xA6, "agent_addr": None, "uptime": None}
    if pdu_t == 0xA4:  # SNMPv1 Trap-PDU
        enterprise = _oid(p[0][1])
        out["agent_addr"] = _val(*p[1])
        generic, specific = _val(*p[2]), _val(*p[3])
        out["uptime"] = _val(*p[4])
        out["trap_oid"] = GENERIC_V1[generic] if 0 <= generic < 6 else f"{enterprise}.0.{specific}"
        vbs = p[5][1]
    elif pdu_t in (0xA7, 0xA6):  # SNMPv2-Trap / Inform
        vbs = p[3][1]
        out["trap_oid"] = None
    else:
        raise ValueError(f"not a trap PDU (0x{pdu_t:x})")
    varbinds = []
    for _, vb in _seq(vbs):
        (_, ov), (vt, vv) = _seq(vb)
        oid, val = _oid(ov), _val(vt, vv)
        if oid == TRAP_OID:
            out["trap_oid"] = val
        elif oid == UPTIME_OID:
            out["uptime"] = val
        else:
            varbinds.append((oid, val))
    out["varbinds"] = varbinds
    return out


def trap_to_event(trap: dict) -> dict:
    oid = trap.get("trap_oid") or "unknown"
    label, emoji, sev = TRAPS.get(oid, (f"Trap {oid}", "📡", "info"))
    named = {oid_name(o).split(".")[0]: v for o, v in trap["varbinds"]}
    iface = named.get("ifName") or named.get("ifDescr")
    lines = [f"{emoji} **{label}**" + (f" on `{iface}`" if iface else "")]
    if named.get("ifAlias"):
        lines.append(f"“{named['ifAlias']}”")
    for o, v in trap["varbinds"][:6]:
        n = oid_name(o)
        if n.split(".")[0] not in ("ifName", "ifDescr", "ifAlias"):
            lines.append(f"`{n}` = {v}")
    return {"content": "\n".join(lines), "kind": "alert" if sev in ("warning", "error", "critical") else "log",
            "severity": sev, "meta": {"source": "snmp", "snmp": {**{k: trap[k] for k in ("version", "trap_oid", "uptime")},
                                                                  "varbinds": [[o, str(v)] for o, v in trap["varbinds"][:20]]}}}


# ---------------------------------------------------------------- syslog
SEV_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
_PRI = re.compile(r"^<(\d{1,3})>(.*)$", re.S)
_R5424 = re.compile(r"^1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:\[.*?\])+) ?(.*)$", re.S)
_R3164 = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2} \d\d:\d\d:\d\d) (\S+) (.*)$", re.S)
_TAG = re.compile(r"^([\w./-]{1,48})(?:\[\d+\])?: ?(.*)$", re.S)


def parse_syslog(data: bytes) -> dict:
    text = data.decode("utf-8", "replace").strip().lstrip("\ufeff")
    m = _PRI.match(text)
    pri, rest = (int(m.group(1)), m.group(2)) if m else (13, text)
    fac, sev = divmod(pri, 8)
    host = app = None
    if (m5 := _R5424.match(rest)):
        host, app, msg = m5.group(2), m5.group(3), m5.group(7)
        host, app = (None if host == "-" else host), (None if app == "-" else app)
    elif (m3 := _R3164.match(rest)):
        host, msg = m3.group(2), m3.group(3)
        if (t := _TAG.match(msg)):
            app, msg = t.group(1), t.group(2)
    else:
        msg = rest
    return {"facility": fac, "severity": sev, "host": host, "app": app, "message": msg.strip().lstrip("\ufeff")}


def syslog_to_event(p: dict) -> dict:
    sev = p["severity"]
    level = "critical" if sev <= 2 else "error" if sev == 3 else "warning" if sev == 4 else "info"
    emoji = {"critical": "🔥", "error": "🔴", "warning": "⚠️"}.get(level, "📜")
    content = f"{emoji} [{SEV_NAMES[sev]}] " + (f"{p['app']}: " if p["app"] else "") + p["message"]
    return {"content": content[:2000], "kind": "alert" if sev <= 4 else "log", "severity": level,
            "meta": {"source": "syslog", "syslog": {k: p[k] for k in ("facility", "severity", "host", "app")}}}


# ---------------------------------------------------------------- servers
class _UDP(asyncio.DatagramProtocol):
    def __init__(self, net, handler) -> None:
        self.net, self.handler = net, handler

    def datagram_received(self, data: bytes, addr) -> None:
        self.net.spawn(self.handler(self.net, data, addr[0]))


async def _on_syslog(net, data: bytes, ip: str) -> None:
    try:
        p = parse_syslog(data)
    except Exception as e:
        log.debug("bad syslog from %s: %s", ip, e)
        return
    if p["severity"] > net.s.syslog_min_severity:
        return
    await net.device_event(ip, "syslog", syslog_to_event(p), hint=p["host"])


async def _on_trap(net, data: bytes, ip: str) -> None:
    try:
        trap = parse_trap(data)
    except Exception as e:
        log.debug("bad trap from %s: %s", ip, e)
        return
    if net.s.snmp_communities and trap["community"] not in net.s.snmp_communities:
        log.info("dropped trap from %s: community not allowed", ip)
        return
    await net.device_event(ip, "snmp", trap_to_event(trap))


async def start_listeners(net) -> list:
    loop, transports = asyncio.get_running_loop(), []
    for port, handler, label in ((net.s.snmp_port, _on_trap, "SNMP traps"), (net.s.syslog_port, _on_syslog, "syslog")):
        if not port:
            continue
        try:
            tr, _ = await loop.create_datagram_endpoint(lambda h=handler: _UDP(net, h), local_addr=(net.s.udp_host, port))
            transports.append(tr)
            log.info("listening for %s on udp/%s", label, port)
        except OSError as e:
            log.error("could not bind %s listener on udp/%s: %s", label, port, e)
    return transports
