"""Phone calls against a simulated Asterisk ARI (no PBX needed)."""
import os

os.environ["AGENTCHAT_NO_AUTOAPP"] = "1"

import asyncio

import httpx
from fastapi.testclient import TestClient

from app.main import Settings, create_app

ADMIN = "t"


class FakeARI:
    """Stands in for Telephony.ari(): answers REST calls and feeds ARI events back into dispatch()."""

    def __init__(self, tel, caller_lines):
        self.tel, self.lines, self.log = tel, list(caller_lines), []

    def emit(self, ev, delay=0.05):
        async def later():
            await asyncio.sleep(delay)
            await self.tel.dispatch(ev)
        asyncio.get_running_loop().create_task(later())

    async def __call__(self, method, path, **kw):
        p = kw.get("params", {})
        self.log.append((method, path, p))
        if method == "POST" and path == "/channels":
            cid = p["channelId"]
            if p["endpoint"].endswith("9999"):
                self.emit({"type": "ChannelDestroyed", "channel": {"id": cid}, "cause_txt": "No answer"})
            else:
                self.emit({"type": "StasisStart", "channel": {"id": cid}, "args": p["appArgs"].split(",")})
        elif "/play/" in path:
            cid, pid = path.split("/")[2], path.split("/")[-1]
            self.emit({"type": "PlaybackFinished", "playback": {"id": pid, "target_uri": f"channel:{cid}"}})
        elif path.endswith("/record"):
            cid = path.split("/")[2]
            self.emit({"type": "RecordingFinished", "recording": {"name": p["name"], "target_uri": f"channel:{cid}"}})
        elif path.endswith("/file"):
            return httpx.Response(200, content=b"RIFF" + b"\0" * 5000)
        elif method == "DELETE" and path.startswith("/channels/"):
            self.emit({"type": "StasisEnd", "channel": {"id": path.split("/")[2]}})
        return httpx.Response(204)


class StubLLM:
    model, configured = "stub", True

    async def chat(self, messages, **kw):
        return "**Disk** is at 91 percent. https://grafana/x"


def setup(caller_lines):
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False,
                              ari_url="http://pbx/ari", tts_url="http://tts/v1", stt_url="http://stt/v1",
                              public_url="http://hub:8088", oncall_number="1001", calls_per_hour=3))
    net = app.state.net
    net.llm = StubLLM()
    tel = net.tel

    async def no_ws():  # don't open a real ARI websocket in tests
        return None
    tel.run = no_ws
    tel.connected = True
    fake = FakeARI(tel, caller_lines)
    tel.ari = fake
    heard = list(caller_lines)

    async def tts(text, agent):
        return b"RIFF-fake-8k-wav"

    async def stt(wav):
        return heard.pop(0) if heard else ""
    tel.tts, tel.stt = tts, stt
    return app, net, fake


def h(t):
    return {"Authorization": f"Bearer {t}"}


def wait_until(c, cid, states=("ended", "failed", "no-answer"), tries=60):
    import time
    for _ in range(tries):
        call = c.get(f"/api/calls/{cid}", headers=h(ADMIN)).json()
        if call["state"] in states:
            return call
        time.sleep(0.05)
    return call


def test_outbound_two_way_call_with_transcript():
    app, net, fake = setup(["How full is the disk?", "Thanks, bye"])
    with TestClient(app) as c:
        key = c.post("/api/onboard", json={"name": "sentinel", "kind": "llm"}, headers=h(ADMIN)).json()["api_key"]
        r = c.post("/api/calls", json={"to": "1001", "message": "Heads up about nas01.", "conversation": True}, headers=h(key))
        assert r.status_code == 201, r.text
        call = wait_until(c, r.json()["id"])
        assert call["state"] == "ended" and call["turns"] == 2 and call["direction"] == "out"
        lines = [(m["author_name"], m["content"]) for m in c.get(f"/api/posts/{call['thread']}/thread", headers=h(ADMIN)).json()]
        assert lines[0][1].startswith("📞 Calling **1001**")
        assert ("sentinel", "Heads up about nas01.") in lines
        assert ("phone-1001", "🗣 How full is the disk?") in lines
        assert ("sentinel", "Disk is at 91 percent.") in lines  # markdown + URL stripped for speech
        assert lines[-1][1].startswith("📴 Call ended")
        dial = next(x for x in fake.log if x[1] == "/channels")[2]
        assert dial["endpoint"] == "PJSIP/1001" and dial["appArgs"].startswith("out,")
        plays = [x[2]["media"] for x in fake.log if "/play/" in x[1]]
        assert plays and all(m.startswith("sound:http://hub:8088/media/") and m.endswith(".wav") for m in plays)
        mid = plays[0].rsplit("/", 1)[1][:-4]
        assert c.get(f"/media/{mid}.wav").content == b"RIFF-fake-8k-wav"


def test_inbound_call_and_guards():
    app, net, fake = setup(["Is the backup done?", "goodbye"])
    with TestClient(app) as c:
        key = c.post("/api/onboard", json={"name": "hermes", "kind": "llm"}, headers=h(ADMIN)).json()["api_key"]
        c.portal.call(net.tel.dispatch, {"type": "StasisStart", "channel": {"id": "in-1", "caller": {"number": "5551234"}},
                                         "args": ["in", "hermes"]})
        cid = c.get("/api/calls", headers=h(ADMIN)).json()[0]["id"]
        call = wait_until(c, cid)
        assert call["direction"] == "in" and call["number"] == "5551234" and call["turns"] == 2
        assert ("POST", "/channels/in-1/answer", {}) in fake.log

        bad = c.post("/api/calls", json={"to": "+18005550000", "message": "hi"}, headers=h(key))
        assert bad.status_code == 403  # outside AGENTCHAT_CALL_ALLOW
        assert c.post("/api/calls", json={"to": "1001"}, headers=h(key)).status_code == 422
        r = c.post("/api/calls", json={"to": "9999", "message": "anyone?"}, headers=h(key))
        assert wait_until(c, r.json()["id"])["state"] == "no-answer"
        c.post("/api/calls", json={"to": "1002", "message": "a"}, headers=h(key))
        c.post("/api/calls", json={"to": "1003", "message": "b"}, headers=h(key))
        assert c.post("/api/calls", json={"to": "1004", "message": "c"}, headers=h(key)).status_code == 429
        assert c.get("/api/config").json()["phone"]["connected"] is True


def test_critical_alert_escalates_to_oncall():
    app, net, fake = setup(["what happened?", "bye"])
    with TestClient(app) as c:
        res = c.post("/api/onboard", json={"name": "grafana", "kind": "webhook"}, headers=h(ADMIN)).json()
        c.post(f"/ingest/{res['ingest_token']}", json={"alerts": [{"status": "firing", "labels": {
            "alertname": "NASDown", "severity": "critical", "instance": "nas01"}}]})
        import time
        for _ in range(60):
            calls = c.get("/api/calls", headers=h(ADMIN)).json()
            if calls:
                break
            time.sleep(0.05)
        assert calls and calls[0]["number"] == "1001" and calls[0]["reason"] == "critical alert escalation"
        # cooldown: a second critical alert from the same agent doesn't call again
        c.post(f"/ingest/{res['ingest_token']}", json={"alerts": [{"status": "firing", "labels": {
            "alertname": "NASDown", "severity": "critical"}}]})
        time.sleep(0.3)
        assert len(c.get("/api/calls", headers=h(ADMIN)).json()) == 1


def test_phone_off_by_default():
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False))
    with TestClient(app) as c:
        k = c.post("/api/onboard", json={"name": "x"}, headers=h(ADMIN)).json()["api_key"]
        assert c.post("/api/calls", json={"to": "1001", "message": "hi"}, headers=h(k)).status_code == 503
        assert c.get("/api/config").json()["phone"]["enabled"] is False
