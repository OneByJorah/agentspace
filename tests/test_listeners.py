"""Parsers for SNMP traps and syslog — traps are hand-encoded in BER to avoid any SNMP dependency."""
from app.ingest import format_webhook
from app.listeners import parse_syslog, parse_trap, syslog_to_event, trap_to_event


def tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    ln = bytes([n]) if n < 0x80 else bytes([0x82]) + n.to_bytes(2, "big")
    return bytes([tag]) + ln + body


def oid(s: str) -> bytes:
    parts = [int(x) for x in s.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.insert(0, 0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(chunk)
    return tlv(0x06, bytes(out))


def integer(n: int) -> bytes:
    return tlv(0x02, n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=True))


def vb(o: str, val: bytes) -> bytes:
    return tlv(0x30, oid(o) + val)


def v2c_trap(trap_oid: str, extra: list[bytes]) -> bytes:
    vbs = tlv(0x30, vb("1.3.6.1.2.1.1.3.0", tlv(0x43, (123456).to_bytes(3, "big")))
              + vb("1.3.6.1.6.3.1.1.4.1.0", oid(trap_oid)) + b"".join(extra))
    pdu = tlv(0xA7, integer(42) + integer(0) + integer(0) + vbs)
    return tlv(0x30, integer(1) + tlv(0x04, b"public") + pdu)


def test_v2c_link_down_trap():
    data = v2c_trap("1.3.6.1.6.3.1.1.5.3", [vb("1.3.6.1.2.1.2.2.1.1.3", integer(3)),
                                             vb("1.3.6.1.2.1.2.2.1.2.3", tlv(0x04, b"GigabitEthernet0/3")),
                                             vb("1.3.6.1.2.1.2.2.1.8.3", integer(2))])
    t = parse_trap(data)
    assert t["version"] == "v2c" and t["community"] == "public" and t["uptime"] == 123456
    assert t["trap_oid"] == "1.3.6.1.6.3.1.1.5.3"
    ev = trap_to_event(t)
    assert "Link DOWN" in ev["content"] and "GigabitEthernet0/3" in ev["content"]
    assert ev["severity"] == "error" and ev["kind"] == "alert"


def test_v1_trap():
    pdu = tlv(0xA4, oid("1.3.6.1.4.1.9") + tlv(0x40, bytes([10, 0, 0, 1])) + integer(4) + integer(0)
              + tlv(0x43, b"\x01\x00") + tlv(0x30, b""))
    t = parse_trap(tlv(0x30, integer(0) + tlv(0x04, b"lab") + pdu))
    assert t["version"] == "v1" and t["agent_addr"] == "10.0.0.1" and t["trap_oid"] == "1.3.6.1.6.3.1.1.5.5"
    assert "Authentication failure" in trap_to_event(t)["content"]


def test_enterprise_trap_and_garbage():
    t = parse_trap(v2c_trap("1.3.6.1.4.1.14823.2.3.3.1.200.1", []))
    assert trap_to_event(t)["severity"] == "info"
    import pytest
    with pytest.raises(Exception):
        parse_trap(b"\x00\x01garbage")


def test_syslog_rfc3164_and_5424():
    p = parse_syslog(b"<11>Nov 14 09:12:01 nas01 smartd[812]: Device /dev/sda failing")
    assert (p["severity"], p["host"], p["app"]) == (3, "nas01", "smartd")
    assert syslog_to_event(p)["severity"] == "error"
    p = parse_syslog(b"<165>1 2026-11-14T10:00:00Z fw01 filterlog 123 - - blocked 1.2.3.4")
    assert (p["severity"], p["host"], p["app"], p["message"]) == (5, "fw01", "filterlog", "blocked 1.2.3.4")
    assert parse_syslog(b"no pri at all")["message"] == "no pri at all"


def test_webhook_formatter_fallbacks():
    assert format_webhook(b"", {}, {})[0]["content"] == "(empty payload)"
    ev = format_webhook(b'{"embeds":[{"title":"T","description":"D"}]}', {}, {})[0]
    assert "T" in ev["content"] and "D" in ev["content"]
