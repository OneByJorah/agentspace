"""Autopilot brain + LLM persona generation, with a stub LLM."""
import os

os.environ["VIGILO_NO_AUTOAPP"] = "1"

import json

from fastapi.testclient import TestClient

from app.brain import Brain
from app.main import Settings, create_app

ADMIN = "t"


class StubLLM:
    model = "stub"
    configured = True

    def __init__(self):
        self.calls = []

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        last = messages[-1]["content"]
        if "Return ONLY a JSON" in last:
            return 'Sure! {"display_name": "Nova", "bio": "🌟 star", "mood": "bright", "tags": ["a", "b"], ' \
                   '"first_post": "hi all #newhere", "system_prompt": "You are Nova."}'
        if "Write ONE new post" in last:
            return "Thinking out loud about uptime. #ops"
        return "nova: <think>hmm</think>Got it, on it."


def make():
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False,
                              brain_max_depth=2))
    stub = StubLLM()
    app.state.net.llm = stub
    return app, stub


def h(t):
    return {"Authorization": f"Bearer {t}"}


def test_llm_persona_and_autopilot_reply_and_autopost():
    app, stub = make()
    net = app.state.net
    with TestClient(app) as c:
        res = c.post("/api/onboard", json={"name": "nova", "kind": "llm", "autopilot": True, "post_interval_min": 1},
                     headers=h(ADMIN)).json()
        assert res["llm_persona"] and res["agent"]["display_name"] == "Nova" and res["persona"]["first_post"] == "hi all #newhere"
        assert "You are Nova." in res["persona"]["system_prompt"]

        brain = Brain(net)
        q = c.post("/api/posts", json={"content": "@nova status?"}, headers=h(ADMIN)).json()
        a = net.agent("nova")
        c.portal.call(brain.respond, a)
        thread = c.get(f"/api/posts/{q['id']}/thread", headers=h(ADMIN)).json()
        reply = thread[-1]
        assert reply["author_name"] == "nova" and reply["content"] == "Got it, on it." and reply["meta"]["autopilot"]
        assert "from @operator:\n@nova status?" in stub.calls[-1][-1]["content"]

        # DMs get answered too
        c.post("/api/dms/nova", json={"content": "psst"}, headers=h(ADMIN))
        c.portal.call(brain.respond, net.agent("nova"))
        assert c.get("/api/dms/nova", headers=h(ADMIN)).json()[-1]["author_name"] == "nova"

        # spontaneous post once the interval has elapsed
        net.store.run("UPDATE agents SET last_autopost=0, created_at=0 WHERE name='nova'")
        c.portal.call(brain.maybe_post, net.agent("nova"))
        feed = c.get("/api/feed", params={"scope": "agent", "name": "nova"}, headers=h(ADMIN)).json()
        assert feed[0]["content"] == "Thinking out loud about uptime. #ops"


def test_brain_respects_depth_guard_between_agents():
    app, stub = make()
    net = app.state.net
    with TestClient(app) as c:
        k = c.post("/api/onboard", json={"name": "other"}, headers=h(ADMIN)).json()["api_key"]
        c.post("/api/onboard", json={"name": "nova", "autopilot": True}, headers=h(ADMIN))
        brain = Brain(net)
        c.portal.call(brain.respond, net.agent("nova"))  # drain birth notifications
        p = None
        for i in range(3):  # depth 0,1,2 by "other"
            p = c.post("/api/posts", json={"content": f"@nova {i}", "reply_to": p}, headers=h(k)).json()["id"]
        n_before = len(stub.calls)
        c.portal.call(brain.respond, net.agent("nova"))
        # depth-0 and depth-1 mentions answered; the depth-2 one from another agent is skipped
        assert len(stub.calls) - n_before == 2
        assert json.loads(net.store.one("SELECT mentions FROM messages WHERE id=?", (p,))["mentions"]) == ["nova"]
