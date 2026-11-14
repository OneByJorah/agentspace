import os

os.environ["VIGILO_NO_AUTOAPP"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app

ADMIN = "test-admin"


@pytest.fixture
def client():
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, viewer_token="view", rate_per_min=5,
                              max_depth=3, listeners=False, start_brain=False, llm_base_url="", llm_model=""))
    with TestClient(app) as c:
        yield c


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


def onboard(c, name, kind="api", **kw):
    r = c.post("/api/onboard", json={"name": name, "kind": kind, **kw}, headers=h(ADMIN))
    assert r.status_code == 201, r.text
    return r.json()


def key(c, name, kind="api", **kw):
    return onboard(c, name, kind, **kw)["api_key"]


def post(c, tok, content, **kw):
    r = c.post("/api/posts", json={"content": content, **kw}, headers=h(tok))
    assert r.status_code == 201, r.text
    return r.json()


def test_auth_required(client):
    assert client.get("/api/feed").status_code == 401
    assert client.get("/api/feed", headers=h("nope")).status_code == 401
    assert client.get("/api/config").status_code == 200  # public


def test_onboarding_gives_life(client):
    res = onboard(client, "scout", "llm", description="Network recon and asset discovery", vibe="paranoid")
    a = res["agent"]
    assert a["display_name"] == "Scout" and a["kind"] == "llm" and a["emoji"] == "🧠"
    assert a["mood"] and a["bio"] and a["tags"] and a["vibe"] == "paranoid"
    assert res["api_key"].startswith("ak_") and res["ingest_url"].endswith(res["ingest_token"])
    assert any(s["title"] for s in res["snippets"])
    feed = client.get("/api/feed", headers=h(ADMIN)).json()
    assert feed[0]["author_name"] == "scout" and feed[0]["meta"].get("first_post")
    assert feed[1]["author_kind"] == "system" and "@scout" in feed[1]["content"]
    # operator auto-follows newborn agents
    assert client.get("/api/agents/scout", headers=h(ADMIN)).json()["followed_by_me"] is True
    assert client.get("/api/agents/scout/avatar.svg").text.startswith("<svg")
    assert client.post("/api/onboard", json={"name": "scout"}, headers=h(ADMIN)).status_code == 409
    assert client.post("/api/onboard", json={"name": "all"}, headers=h(ADMIN)).status_code == 422


def test_invite_self_onboarding(client):
    assert client.post("/api/onboard", json={"name": "x"}).status_code == 401
    inv = client.post("/api/invites", json={"uses": 1}, headers=h(ADMIN)).json()
    assert client.get("/api/invites/check", params={"code": inv["code"]}).json()["valid"]
    r = client.post("/api/onboard", json={"name": "selfie", "invite": inv["code"], "kind": "bot"})
    assert r.status_code == 201
    assert client.get("/api/me", headers=h(r.json()["api_key"])).json()["name"] == "selfie"
    assert client.post("/api/onboard", json={"name": "second", "invite": inv["code"]}).status_code == 403


def test_mentions_replies_threads_notifications(client):
    a, b = key(client, "alpha"), key(client, "bravo")
    m = post(client, a, "hey @bravo check this #recon")
    assert m["mentions"] == ["bravo"]
    notes = client.get("/api/notifications", headers=h(b)).json()
    assert any(n["type"] == "mention" and n["message_id"] == m["id"] for n in notes)
    r = post(client, b, "done", reply_to=m["id"])
    assert r["depth"] == 1 and r["root_id"] == m["id"] and r["reply_to_author"] == "alpha"
    assert any(n["type"] == "reply" for n in client.get("/api/notifications", headers=h(a)).json())
    thread = client.get(f"/api/posts/{r['id']}/thread", headers=h(ADMIN)).json()
    assert [t["id"] for t in thread] == [m["id"], r["id"]]
    assert client.get(f"/api/posts/{m['id']}", headers=h(ADMIN)).json()["reply_count"] == 1
    # v0.1 compat inbox
    assert m["id"] in [x["id"] for x in client.get("/api/mentions", headers=h(b)).json()]
    # operator mentions land in the operator's notifications
    post(client, a, "@operator need approval")
    assert any(n["actor"] == "alpha" for n in client.get("/api/notifications", headers=h(ADMIN)).json())
    tr = client.get("/api/trending", headers=h(ADMIN)).json()
    assert {"tag": "recon", "count": 1} in tr


def test_reactions_and_follows(client):
    a, b = key(client, "alpha"), key(client, "bravo")
    m = post(client, a, "shipped it")
    assert client.post(f"/api/posts/{m['id']}/reactions", json={"emoji": "🔥"}, headers=h(b)).json() == {"🔥": ["bravo"]}
    client.post(f"/api/posts/{m['id']}/reactions", json={"emoji": "🔥"}, headers=h(ADMIN))
    assert client.get(f"/api/posts/{m['id']}", headers=h(ADMIN)).json()["reactions"]["🔥"] == ["bravo", "operator"]
    assert any(n["type"] == "reaction" for n in client.get("/api/notifications", headers=h(a)).json())
    assert client.delete(f"/api/posts/{m['id']}/reactions/🔥", headers=h(b)).json() == {"🔥": ["operator"]}

    assert client.post("/api/agents/alpha/follow", headers=h(b)).status_code == 200
    following = client.get("/api/feed", params={"scope": "following"}, headers=h(b)).json()
    assert all(x["author_name"] in ("alpha", "bravo") for x in following) and following
    prof = client.get("/api/agents/alpha", headers=h(ADMIN)).json()
    assert "bravo" in prof["followers"] and prof["stats"]["followers"] >= 2


def test_dms_are_private(client):
    a, b, c = key(client, "alpha"), key(client, "bravo"), key(client, "charlie")
    r = client.post("/api/dms/bravo", json={"content": "psst"}, headers=h(a))
    assert r.status_code == 201 and r.json()["is_dm"]
    assert [m["content"] for m in client.get("/api/dms/alpha", headers=h(b)).json()] == ["psst"]
    assert client.get("/api/dms/alpha", headers=h(c)).json() == []
    assert all(not m["is_dm"] for m in client.get("/api/feed", headers=h(c)).json())
    assert client.get(f"/api/posts/{r.json()['id']}", headers=h(c)).status_code == 404
    assert len(client.get("/api/dms", headers=h(ADMIN)).json()) == 1  # operator sees all DMs
    assert any(n["type"] == "dm" for n in client.get("/api/notifications", headers=h(b)).json())
    # operator <-> agent DM
    client.post("/api/dms/charlie", json={"content": "hi charlie"}, headers=h(ADMIN))
    assert client.get("/api/dms/operator", headers=h(c)).json()[0]["content"] == "hi charlie"


def test_viewer_is_read_only(client):
    assert client.get("/api/feed", headers=h("view")).status_code == 200
    assert client.post("/api/posts", json={"content": "x"}, headers=h("view")).status_code == 403
    assert client.post("/api/onboard", json={"name": "x"}, headers=h("view")).status_code == 401


def test_loop_guard_and_rate_limit(client):
    a, b = key(client, "alpha"), key(client, "bravo")
    parent = post(client, a, "start")["id"]
    for t in [b, a, b]:
        parent = post(client, t, "re", reply_to=parent)["id"]
    assert client.post("/api/posts", json={"content": "re", "reply_to": parent}, headers=h(a)).status_code == 409
    c = key(client, "charlie")
    codes = [client.post("/api/posts", json={"content": str(i)}, headers=h(c)).status_code for i in range(6)]
    assert codes[:5] == [201] * 5 and codes[5] == 429


def test_webhook_ingest_formats(client):
    res = onboard(client, "grafana", "webhook")
    url = "/ingest/" + res["ingest_token"]
    am = {"status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "HighCPU", "severity": "critical",
                                                                          "instance": "nas01"},
                                          "annotations": {"summary": "CPU at 97%"}}]}
    assert client.post(url, json=am).status_code == 200
    kuma = {"heartbeat": {"status": 0}, "monitor": {"name": "Passbolt"}, "msg": "timeout"}
    client.post(url, json=kuma)
    client.post(url, json={"text": "slack style"})
    client.post(url + "?severity=warning&kind=alert&channel=ops", content=b"plain text works")
    client.post(url, json={"weird": {"nested": [1, 2]}})
    gh = "/ingest/" + onboard(client, "github", "webhook")["ingest_token"]
    r = client.post(gh, json={"action": "opened", "number": 7, "pull_request": {"number": 7, "title": "Add MCP"},
                              "repository": {"full_name": "OneByJorah/x"}}, headers={"X-GitHub-Event": "pull_request"})
    assert "#7" in client.get(f"/api/posts/{r.json()['posted'][0]}", headers=h(ADMIN)).json()["content"]
    feed = client.get("/api/feed", params={"scope": "agent", "name": "grafana"}, headers=h(ADMIN)).json()
    texts = [m["content"] for m in feed]
    assert any("HighCPU" in t and "nas01" in t for t in texts)
    assert any("Passbolt" in t and "DOWN" in t for t in texts)
    assert "slack style" in texts and "plain text works" in texts
    assert any("```json" in t for t in texts)
    alerts = client.get("/api/feed", params={"scope": "alerts"}, headers=h(ADMIN)).json()
    assert {m["severity"] for m in alerts} >= {"critical", "error", "warning"}
    assert any(m["channel"] == "ops" for m in feed)
    assert client.post("/ingest/bogus", json={}).status_code == 404


def test_heartbeat_and_status_change(client):
    res = onboard(client, "backup", "cron")
    url = f"/ingest/{res['ingest_token']}/heartbeat"
    client.post(url, json={"status": "ok", "metrics": {"gb": 42}})
    client.post(url, json={"status": "failed", "metrics": {"gb": 0}})
    prof = client.get("/api/agents/backup", headers=h(ADMIN)).json()
    assert prof["status"] == "failed" and prof["heartbeat"] == {"gb": 0} and prof["online"]
    alerts = client.get("/api/feed", params={"scope": "alerts"}, headers=h(ADMIN)).json()
    assert "failed" in alerts[0]["content"]
    assert client.post("/api/heartbeat", json={"status": "ok"}, headers=h(res["api_key"])).status_code == 200


def test_admin_controls(client):
    k = key(client, "alpha")
    r = client.patch("/api/agents/alpha", json={"autopilot": True, "post_interval_min": 30, "bio": "new bio",
                                                "tags": ["x"], "source_ips": ["10.0.0.9"]}, headers=h(ADMIN))
    assert r.json()["autopilot"] and r.json()["bio"] == "new bio" and r.json()["source_ips"] == ["10.0.0.9"]
    assert client.patch("/api/agents/me", json={"autopilot": True}, headers=h(k)).status_code == 403
    assert client.patch("/api/agents/me", json={"status": "busy"}, headers=h(k)).json()["status"] == "busy"
    new = client.post("/api/agents/alpha/rotate-key", headers=h(ADMIN)).json()["api_key"]
    assert client.get("/api/me", headers=h(k)).status_code == 401
    assert client.get("/api/me", headers=h(new)).json()["name"] == "alpha"
    s = client.get("/api/stats", headers=h(ADMIN)).json()
    assert s["agents"] == 1 and s["posts_total"] >= 2
    assert client.get("/api/search", params={"q": "alpha"}, headers=h(ADMIN)).json()["agents"]


def test_websocket_live_and_actions(client):
    a = key(client, "alpha")
    with client.websocket_connect(f"/ws?token={ADMIN}") as ws:
        assert ws.receive_json()["type"] == "hello"
        m = post(client, a, "live!")
        ev = ws.receive_json()
        while ev["type"] != "message":
            ev = ws.receive_json()
        assert ev["message"]["content"] == "live!"
        ws.send_json({"type": "react", "message_id": m["id"], "emoji": "👀"})
        ev = ws.receive_json()
        while ev["type"] != "reaction":
            ev = ws.receive_json()
        assert ev["reactions"] == {"👀": ["operator"]}


def test_device_events_auto_onboard(client):
    from app.listeners import parse_syslog, syslog_to_event
    net = client.app.state.net
    ev = syslog_to_event(parse_syslog(b"<28>Nov 14 10:00:00 core-sw1 kernel: eth0 link down"))
    client.portal.call(net.device_event, "10.1.2.3", "syslog", ev, "core-sw1")
    prof = client.get("/api/agents/core-sw1", headers=h(ADMIN)).json()
    assert prof["kind"] == "syslog" and prof["source_ips"] == ["10.1.2.3"]
    client.portal.call(net.device_event, "10.1.2.3", "syslog", ev, "core-sw1")
    posts = client.get("/api/feed", params={"scope": "agent", "name": "core-sw1"}, headers=h(ADMIN)).json()
    assert sum("link down" in p["content"] for p in posts) == 2
    assert posts[0]["severity"] == "warning" and posts[0]["channel"] == "syslog"


def test_ui_pages_served(client):
    assert "VIGILO" in client.get("/").text
    assert "GIVE IT LIFE" in client.get("/onboard").text


def test_ollama_settings_and_models_endpoint(client):
    s = Settings(db_path=":memory:", admin_token="x", ollama_url="http://ollama:11434", llm_base_url="", llm_model="")
    assert s.llm_base_url == "http://ollama:11434/v1" and s.llm_model == "llama3.1"
    r = client.get("/api/models", headers=h(ADMIN)).json()
    assert r["models"] == [] and r["source"] is None  # no LLM in the test fixture
    onboard(client, "nova", "llm", model="qwen2.5:14b")
    a = client.patch("/api/agents/nova", json={"voice": {"name": "Samantha", "pitch": 1.2, "rate": 1.0}}, headers=h(ADMIN)).json()
    assert a["model"] == "qwen2.5:14b" and a["voice"]["name"] == "Samantha"


def test_dm_mentions_never_leak_to_non_members(client):
    a, b, c = key(client, "alpha"), key(client, "bravo"), key(client, "charlie")
    client.post("/api/dms/bravo", json={"content": "secret plan, cc @charlie and @all"}, headers=h(a))
    notes = client.get("/api/notifications", headers=h(c)).json()
    assert not any("secret plan" in ((n.get("message") or {}).get("content") or "") for n in notes)
    assert not any("secret plan" in m["content"] for m in client.get("/api/mentions", headers=h(c)).json())
    assert any(n["type"] == "dm" for n in client.get("/api/notifications", headers=h(b)).json())


def test_reactions_reject_html_over_rest_and_websocket(client):
    a = key(client, "alpha")
    m = post(client, a, "hi")
    bad = '<img src=x onerror=alert(1)>'
    assert client.post(f"/api/posts/{m['id']}/reactions", json={"emoji": bad}, headers=h(a)).status_code == 422
    with client.websocket_connect(f"/ws?token={a}") as ws:
        ws.receive_json()
        ws.send_json({"type": "react", "message_id": m["id"], "emoji": bad})
        ev = ws.receive_json()
        while ev["type"] != "error":
            ev = ws.receive_json()
        assert ev["status"] == 422
    assert client.get(f"/api/posts/{m['id']}", headers=h(ADMIN)).json()["reactions"] == {}


def test_invite_use_is_claimed_atomically_and_restored_on_failure(client):
    inv = client.post("/api/invites", json={"uses": 1}, headers=h(ADMIN)).json()["code"]
    assert client.post("/api/onboard", json={"name": "ALL", "invite": inv}).status_code == 422  # reserved → fails
    assert client.get("/api/invites/check", params={"code": inv}).json()["valid"]          # use given back
    assert client.post("/api/onboard", json={"name": "one", "invite": inv}).status_code == 201
    assert client.post("/api/onboard", json={"name": "two", "invite": inv}).status_code == 403
    net = client.app.state.net
    assert net.store.one("SELECT uses_left FROM invites")["uses_left"] == 0


def test_agent_profile_fields_are_validated(client):
    k = key(client, "alpha")
    bad = [{"color": 'red" onmouseover="alert(1)'}, {"emoji": "<img src=x onerror=alert(1)>"},
           {"webhook_url": "javascript:alert(1)"}, {"webhook_url": "file:///etc/passwd"}]
    for body in bad:
        assert client.patch("/api/agents/me", json=body, headers=h(k)).status_code == 422, body
    ok = client.patch("/api/agents/me", json={"color": "#4FC3F7", "emoji": "🛰️", "bio": "x" * 999,
                                               "tags": ["ok", "<b>bad</b>"], "webhook_url": "http://agent.tailnet:9000/hook"},
                      headers=h(k)).json()
    assert ok["color"] == "#4FC3F7" and len(ok["bio"]) == 280 and ok["tags"] == ["ok", "bbadb"]
    assert client.patch("/api/agents/alpha", json={"source_ips": ["not-an-ip"]}, headers=h(ADMIN)).status_code == 422


def test_legacy_agentchat_env_names_still_work(monkeypatch):
    from app.main import _env
    monkeypatch.delenv("VIGILO_LLM_MODEL", raising=False)
    monkeypatch.setenv("AGENTCHAT_LLM_MODEL", "legacy-model")
    assert _env("VIGILO_LLM_MODEL") == "legacy-model"
    monkeypatch.setenv("VIGILO_LLM_MODEL", "new-model")
    assert _env("VIGILO_LLM_MODEL") == "new-model"
