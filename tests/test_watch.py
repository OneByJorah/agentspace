"""Read-only watch engine: probes, SNMP sensors, uptime, alerts, maintenance, notifiers, discovery, analyst."""
import os

os.environ["AGENTCHAT_NO_AUTOAPP"] = "1"

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.listeners import _oid, _seq, _tlv, _val
from app.main import Settings, create_app
from app.watch import READ_ONLY_PDUS, _ber, _ber_int, _ber_oid, probe_snmp_interfaces, snmp_get_request

ADMIN = "t"
H = {"Authorization": f"Bearer {ADMIN}"}
HOOKS: list = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"hello operator")

    def do_POST(self):  # notification webhook sink
        HOOKS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def web():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeSnmpAgent:
    """Answers GetRequest/GetNextRequest from a dict MIB. Refuses anything else (as a read-only device would be probed)."""

    def __init__(self, mib):
        self.mib = mib
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.pdus = []
        threading.Thread(target=self.loop, daemon=True).start()

    @staticmethod
    def key(o):
        return tuple(int(x) for x in o.split("."))

    def enc(self, v):
        if isinstance(v, tuple):  # ("c64", n) / ("gauge", n)
            tag = {"c64": 0x46, "gauge": 0x42, "ticks": 0x43}[v[0]]
            n = v[1]
            return _ber(tag, n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big"))
        if isinstance(v, int):
            return _ber_int(v)
        return _ber(0x04, str(v).encode())

    def loop(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            _, body, _ = _tlv(data, 0)
            items = _seq(body)
            pdu_t, pdu = items[2]
            self.pdus.append(pdu_t)
            p = _seq(pdu)
            rid = _val(*p[0])
            out = b""
            for _, vb in _seq(p[3][1]):
                (_, ov), _ = _seq(vb)
                oid = _oid(ov)
                if pdu_t == 0xA0:
                    val = self.mib.get(oid)
                    oid_out, venc = oid, (self.enc(val) if val is not None else b"\x80\x00")
                else:
                    nxt = sorted((o for o in self.mib if self.key(o) > self.key(oid)), key=self.key)
                    oid_out, venc = (nxt[0], self.enc(self.mib[nxt[0]])) if nxt else (oid, b"\x82\x00")
                out += _ber(0x30, _ber_oid(oid_out) + venc)
            resp = _ber(0x30, _ber_int(1) + _ber(0x04, b"public") + _ber(0xA2, _ber_int(rid) + _ber_int(0) + _ber_int(0) + _ber(0x30, out)))
            self.sock.sendto(resp, addr)


def mib(in1=1000, oper2=1):
    return {
        "1.3.6.1.2.1.1.1.0": "Aruba 2930F", "1.3.6.1.2.1.1.3.0": ("ticks", 123456), "1.3.6.1.2.1.1.5.0": "core-sw1",
        "1.3.6.1.2.1.2.2.1.2.1": "1/1", "1.3.6.1.2.1.2.2.1.2.2": "1/2",
        "1.3.6.1.2.1.2.2.1.8.1": 1, "1.3.6.1.2.1.2.2.1.8.2": oper2,
        "1.3.6.1.2.1.31.1.1.1.1.1": "1/1", "1.3.6.1.2.1.31.1.1.1.1.2": "1/2",
        "1.3.6.1.2.1.31.1.1.1.6.1": ("c64", in1), "1.3.6.1.2.1.31.1.1.1.6.2": ("c64", 5),
        "1.3.6.1.2.1.31.1.1.1.10.1": ("c64", 2000), "1.3.6.1.2.1.31.1.1.1.10.2": ("c64", 5),
        "1.3.6.1.2.1.31.1.1.1.15.1": ("gauge", 1000), "1.3.6.1.2.1.31.1.1.1.15.2": ("gauge", 1000),
        "1.3.6.1.2.1.31.1.1.1.18.1": "uplink to AP closet", "1.3.6.1.2.1.31.1.1.1.18.2": "",
    }


def test_snmp_is_read_only_by_construction():
    assert set(READ_ONLY_PDUS.values()) == {0xA0, 0xA1}
    with pytest.raises(KeyError):
        snmp_get_request("public", ["1.3.6.1.2.1.1.5.0"], 1, "set")
    import app.watch as w
    src = open(w.__file__).read()
    assert "0xA3" not in src.replace("(0xA3)", "")  # no SetRequest anywhere except the comment saying so


def test_snmp_interface_sensor_bandwidth_and_down():
    agent = FakeSnmpAgent(mib())

    async def go():
        first = await probe_snmp_interfaces("127.0.0.1", "public", None, timeout=2, port=agent.port)
        assert first["ok"] and first["total"] == 2 and first["up"] == 2
        assert first["interfaces"]["1"]["alias"] == "uplink to AP closet"
        prev = {"interfaces": {k: {**v, "ts": v["ts"] - 10} for k, v in first["interfaces"].items()}}
        agent.mib.update(mib(in1=1000 + 12_500_000, oper2=2))  # +100 Mb over 10 s on port 1; port 2 goes down
        second = await probe_snmp_interfaces("127.0.0.1", "public", prev, timeout=2, port=agent.port)
        p1 = second["interfaces"]["1"]
        assert 9.5e6 < p1["in_bps"] < 10.5e6 and p1["util_pct"] == pytest.approx(1.0, abs=0.1)
        assert not second["ok"] and "1/2" in second["error"]
    asyncio.run(go())
    assert set(agent.pdus) <= {0xA0, 0xA1}


@pytest.fixture
def hub(web):
    HOOKS.clear()
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False, watch=False,
                              public_url="http://hub"))
    with TestClient(app) as c:
        yield c, app.state.net, web.server_address[1]


def check(c, aid):
    return c.post(f"/api/assets/{aid}/check", headers=H).json()


def test_website_lifecycle_alerts_analyst_notifier_maintenance(hub):
    c, net, port = hub
    c.post("/api/onboard", json={"name": "analyst", "kind": "llm", "listens_alerts": True}, headers=H)
    c.post("/api/notifiers", json={"name": "hook", "type": "webhook", "config": {"url": f"http://127.0.0.1:{port}/hook"}}, headers=H)
    r = c.post("/api/assets", json={"name": "intranet", "kind": "website", "target": f"http://127.0.0.1:{port}/",
                                    "keyword": "hello", "public": True, "critical": True}, headers=H)
    assert r.status_code == 201, r.text
    aid = r.json()["asset"]["id"]
    assert r.json()["asset"]["checks"] == [{"type": "http", "url": f"http://127.0.0.1:{port}/", "contains": "hello"}]
    a = check(c, aid)
    assert a["state"]["reported"] == "up" and a["uptime"]["24h"] == 100.0 and a["state"]["latency_ms"] is not None

    # keyword disappears → down after 2 rounds (hysteresis), critical asset → critical alert
    c.patch(f"/api/assets/{aid}", json={"checks": [{"type": "http", "url": f"http://127.0.0.1:{port}/", "contains": "nope"}]}, headers=H)
    assert check(c, aid)["state"]["reported"] == "up"
    a = check(c, aid)
    assert a["state"]["reported"] == "down" and a["uptime"]["24h"] < 100
    alerts = c.get("/api/feed", params={"scope": "alerts"}, headers=H).json()
    assert "intranet** is DOWN" in alerts[0]["content"] and alerts[0]["severity"] == "critical" and alerts[0]["author_name"] == "intranet"
    notes = net.store.all("SELECT * FROM notifications WHERE recipient='analyst' AND type='alert'")
    assert notes, "analyst should be asked to review the alert"
    time.sleep(0.3)
    assert any("DOWN" in h["text"] and h["severity"] == "critical" for h in HOOKS)

    # recovery
    c.patch(f"/api/assets/{aid}", json={"checks": [{"type": "http", "url": f"http://127.0.0.1:{port}/"}]}, headers=H)
    assert check(c, aid)["state"]["reported"] == "up"
    assert "back UP" in c.get("/api/feed", params={"scope": "agent", "name": "intranet"}, headers=H).json()[0]["content"]

    # maintenance mutes alerts
    c.patch(f"/api/assets/{aid}", json={"maintenance_minutes": 30, "checks": [{"type": "tcp", "port": free_port()}]}, headers=H)
    before = len(c.get("/api/feed", params={"scope": "alerts"}, headers=H).json())
    check(c, aid)
    a = check(c, aid)
    assert a["state"]["reported"] == "down" and a["in_maintenance"]
    assert len(c.get("/api/feed", params={"scope": "alerts"}, headers=H).json()) == before

    detail = c.get(f"/api/assets/{aid}", headers=H).json()
    assert detail["history"] and detail["daily"]
    st = c.get("/api/status").json()  # public, no auth
    assert st["assets"][0]["name"] == "intranet" and "30d" in st["assets"][0]["uptime"]
    assert "Service status" in c.get("/status").text or c.get("/status").status_code == 200


def test_server_ports_latency_threshold_and_push(hub):
    c, net, port = hub
    closed = free_port()
    r = c.post("/api/assets", json={"name": "nas01", "kind": "server", "target": "127.0.0.1", "ports": [port, closed]}, headers=H).json()
    aid = r["asset"]["id"]
    assert [x["type"] for x in r["asset"]["checks"]] == ["ping", "tcp", "tcp"]
    check(c, aid)
    a = check(c, aid)
    assert a["state"]["reported"] == "degraded" and any(f"tcp/{closed}" in f for f in a["state"]["failing"])
    warn = c.get("/api/feed", params={"scope": "alerts"}, headers=H).json()[0]
    assert "DEGRADED" in warn["content"] and warn["severity"] == "warning"

    # slow threshold → degraded, not down
    c.patch(f"/api/assets/{aid}", json={"checks": [{"type": "tcp", "port": port, "warn_ms": 0.0001}]}, headers=H)
    assert check(c, aid)["state"]["status"] == "degraded"

    # push / heartbeat monitor
    r = c.post("/api/assets", json={"name": "nightly-backup", "kind": "cron", "target": "restic", "grace_min": 1}, headers=H).json()
    pid, ingest = r["asset"]["id"], r["ingest_url"]
    check(c, pid)
    assert check(c, pid)["state"]["reported"] == "down"
    c.post(ingest.replace("http://hub", "") + "/heartbeat", json={"status": "ok"})
    assert check(c, pid)["state"]["reported"] == "up"


def test_switch_snmp_and_discovery(hub):
    c, net, port = hub
    agent = FakeSnmpAgent(mib())
    r = c.post("/api/assets", json={"name": "core-sw1", "kind": "switch", "target": "127.0.0.1",
                                    "checks": [{"type": "snmp", "community": "public", "port": agent.port},
                                               {"type": "snmp_if", "community": "public", "port": agent.port}]}, headers=H).json()
    a = check(c, r["asset"]["id"])
    assert a["state"]["reported"] == "up"
    assert a["state"]["snmp"]["sysName"] == "core-sw1" and len(a["state"]["interfaces"]) == 2

    # read-only discovery of localhost with our web port
    c.post("/api/onboard", json={"name": "scout"}, headers=H)
    res = c.portal.call(net.watch.discover, ["127.0.0.1/32"], [port], "test")
    assert res["found"] == 1 and res["hosts"][0]["ports"] == [port]
    d = c.get("/api/discoveries", headers=H).json()["hosts"]
    assert d[0]["ip"] == "127.0.0.1" and d[0]["registered"]
    assert c.post("/api/discover", json={"cidrs": ["10.0.0.0/8"]}, headers=H).status_code == 422


def test_read_only_charter_in_every_persona(hub):
    c, net, port = hub
    a = c.post("/api/onboard", json={"name": "nova", "kind": "llm"}, headers=H).json()
    assert "READ-ONLY" in a["persona"]["system_prompt"] and "never" in a["persona"]["system_prompt"]
    assert a["agent"]["extension"] == "7100"
    b = c.post("/api/onboard", json={"name": "orion", "kind": "llm"}, headers=H).json()
    assert b["agent"]["extension"] == "7101"
