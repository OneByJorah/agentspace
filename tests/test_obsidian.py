"""Obsidian vault sync: notes, wikilinks, idempotence, prefs, git commit + push to a bare remote."""
import os

os.environ["AGENTCHAT_NO_AUTOAPP"] = "1"

import subprocess

from fastapi.testclient import TestClient

from app.main import Settings, create_app

ADMIN = "t"
H = {"Authorization": f"Bearer {ADMIN}"}


def test_vault_notes_git_and_prefs(tmp_path):
    vault, remote = tmp_path / "vault", tmp_path / "vault.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False, watch=False,
                              tailscale_sync=False, vault_dir=str(vault), vault_git_remote=str(remote), vault_interval=3600))
    net = app.state.net
    with TestClient(app) as c:
        ka = c.post("/api/onboard", json={"name": "hermes", "kind": "llm", "listens_alerts": True}, headers=H).json()["api_key"]
        kb = c.post("/api/onboard", json={"name": "scout", "kind": "llm"}, headers=H).json()["api_key"]
        res = c.post("/api/assets", json={"name": "nas01", "kind": "server", "target": "127.0.0.1"}, headers=H).json()
        alert = c.post("/api/posts", json={"content": "🔴 **NAS01** is DOWN\n• tcp/445 refused", "kind": "alert",
                                           "severity": "error"}, headers={"Authorization": f"Bearer {res['api_key']}"}).json()
        c.post("/api/posts", json={"content": "Likely SMB service crash. Check `systemctl status smbd` on nas01. @scout confirm reachability.",
                                   "reply_to": alert["id"], "meta": {"autopilot": True}}, headers={"Authorization": f"Bearer {ka}"})
        c.post("/api/posts", json={"content": "Morning sweep done #network"}, headers={"Authorization": f"Bearer {kb}"})
        c.post("/api/dms/hermes", json={"content": "psst, the DC logs look odd"}, headers={"Authorization": f"Bearer {kb}"})

        st = c.post("/api/obsidian/sync", headers=H).json()
        assert st["enabled"] and st["notes"] >= 6 and st["error"] is None, st
        root = vault / "AgentSpace"
        daily = next((root / "Daily").glob("*.md")).read_text()
        assert 'type: "agentspace-daily"' in daily and "[[AgentSpace/Agents/scout|@scout]]" in daily and "#network" in daily
        alerts = next((root / "Alerts").glob("*.md")).read_text()
        assert "NAS01 is DOWN" in alerts and "🔍 analysis" in alerts and "[[AgentSpace/Assets/nas01|nas01]]" in alerts \
            or "[[AgentSpace/Agents/nas01|@nas01]]" in alerts
        assert "systemctl status smbd" in alerts
        thread = next((root / "Threads").glob("*.md")).read_text()
        assert 'type: "agentspace-thread"' in thread and "participants" in thread
        assert (root / "DMs" / "hermes--scout.md").read_text().count("psst") == 1
        assert "# " in (root / "Agents" / "hermes.md").read_text()
        assert (root / "Assets" / "nas01.md").exists() and "AgentSpace.md" in [p.name for p in root.iterdir()]

        # idempotent: nothing changed → nothing rewritten
        net.vault.mark_all()
        assert c.portal.call(net.vault.flush) == 0

        # pushed to the bare remote on main (what obsidian-git-livesync's post-receive hook listens to)
        log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
        assert "AgentSpace:" in log
        files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True).stdout
        assert "AgentSpace/DMs/hermes--scout.md" in files

        # prefs: turn DMs off → new DMs are no longer written
        assert c.patch("/api/obsidian", json={"dms": False}, headers=H).json()["prefs"]["dms"] is False
        c.post("/api/dms/hermes", json={"content": "second secret"}, headers={"Authorization": f"Bearer {kb}"})
        c.post("/api/obsidian/sync", headers=H)
        assert "second secret" not in (root / "DMs" / "hermes--scout.md").read_text()
        assert c.get("/api/obsidian", headers=H).json()["recent"]


def test_vault_off_by_default():
    app = create_app(Settings(db_path=":memory:", admin_token=ADMIN, listeners=False, start_brain=False, watch=False,
                              tailscale_sync=False, vault_dir=""))
    with TestClient(app) as c:
        assert c.get("/api/obsidian", headers=H).json()["enabled"] is False
        assert c.post("/api/obsidian/sync", headers=H).status_code == 503
