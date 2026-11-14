"""Tailscale auto-discovery against a realistic `tailscale status --json` document (read-only)."""
import os

os.environ["VIGILO_NO_AUTOAPP"] = "1"

import copy

from fastapi.testclient import TestClient

from app.main import Settings, create_app

ADMIN = "t"
H = {"Authorization": f"Bearer {ADMIN}"}
STATUS = {
    "CurrentTailnet": {"Name": "trankilo.github"},
    "Self": {"ID": "n1", "HostName": "noc-box", "DNSName": "noc-box.tail1.ts.net.", "OS": "linux",
             "TailscaleIPs": ["100.64.0.10", "fd7a::1"], "Online": True, "PrimaryRoutes": ["10.10.0.0/24"]},
    "Peer": {
        "nodekey:a": {"ID": "n2", "HostName": "vide-dc", "DNSName": "vide-dc.tail1.ts.net.", "OS": "windows",
                      "TailscaleIPs": ["100.64.0.11"], "Online": True, "PrimaryRoutes": ["192.168.50.0/24", "0.0.0.0/0"]},
        "nodekey:b": {"ID": "n3", "HostName": "Jhon's iPhone", "OS": "iOS", "TailscaleIPs": ["100.64.0.12"],
                      "Online": False, "LastSeen": "2026-11-14T10:00:00Z"},
        "nodekey:c": {"ID": "n4", "HostName": "big-site", "OS": "linux", "TailscaleIPs": ["100.64.0.13"], "Online": True,
                      "PrimaryRoutes": ["10.0.0.0/8"]},
    },
}


def make(status):
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False, watch=False,
                              tailscale_sync=False))
    net = app.state.net
    state = {"doc": status}

    async def fake_status():
        return state["doc"]
    net.tailscale.status = fake_status
    return app, net, state


def test_tailscale_nodes_routes_and_new_node_alert():
    app, net, state = make(copy.deepcopy(STATUS))
    with TestClient(app) as c:
        c.post("/api/onboard", json={"name": "scout"}, headers=H)
        s = c.post("/api/tailscale/sync", headers=H).json()
        assert len(s["nodes"]) == 4 and s["tailnet"] == "trankilo.github"
        assert s["routes"] == ["10.10.0.0/24", "192.168.50.0/24"]          # default route excluded
        assert s["skipped_routes"] == ["10.0.0.0/8"]                       # too large to sweep
        assert net.watch.extra_cidrs == ["10.10.0.0/24", "192.168.50.0/24"]

        assets = {a["name"]: a for a in c.get("/api/assets", headers=H).json()}
        assert set(assets) == {"noc-box", "vide-dc", "Jhon's iPhone", "big-site"}
        assert assets["vide-dc"]["kind"] == "computer" and assets["noc-box"]["kind"] == "server"
        assert assets["vide-dc"]["checks"][0] == {"type": "tailscale", "node": "n2"}
        assert c.get("/api/agents/jhon-s-iphone", headers=H).status_code == 200

        # offline node → down after hysteresis; optional ping failure alone doesn't degrade an online node
        phone = assets["Jhon's iPhone"]["id"]
        c.post(f"/api/assets/{phone}/check", headers=H)
        a = c.post(f"/api/assets/{phone}/check", headers=H).json()
        assert a["state"]["reported"] == "down" and "offline on Tailscale" in a["state"]["failing"][0]
        dc = c.post(f"/api/assets/{assets['vide-dc']['id']}/check", headers=H).json()
        assert dc["state"]["status"] == "up"

        feed = c.get("/api/feed", params={"scope": "community", "name": "network"}, headers=H).json()
        assert any("Tailscale connected" in m["content"] for m in feed)

        # a new node joins → security alert; re-sync doesn't duplicate assets
        state["doc"]["Peer"]["nodekey:d"] = {"ID": "n5", "HostName": "unknown-laptop", "OS": "windows",
                                             "TailscaleIPs": ["100.64.0.99"], "Online": True}
        c.post("/api/tailscale/sync", headers=H)
        alerts = c.get("/api/feed", params={"scope": "alerts"}, headers=H).json()
        assert any("New node joined the tailnet" in m["content"] and "unknown-laptop" in m["content"] for m in alerts)
        assert len(c.get("/api/assets", headers=H).json()) == 5
