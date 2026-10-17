"""Hosted accounts: each GitHub user gets their own seeded workspace; tokens are scoped to it; usage is capped per user."""
import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app
from server.claude import ClaudeLimitReached


def test_each_user_gets_an_isolated_seeded_workspace(make_core):
    core = make_core(PROJECT_AUTH_MODE="token")
    t1 = core.accounts.sign_in("github", "101", "Alice-Dev", "Alice")
    t2 = core.accounts.sign_in("github", "202", "bob")
    again = core.accounts.sign_in("github", "101", "Alice-Dev")
    p1, p2 = core.projects.authenticate(t1)["project_id"], core.projects.authenticate(t2)["project_id"]
    assert p1 == "u_alice_dev" and p2 == "u_bob" and core.projects.authenticate(again)["project_id"] == p1
    assert {"Report assistant A", "Report assistant B", "Export assistant C"} <= {a["name"] for a in core.coordinator.agents(p1)}
    c = TestClient(create_app(core), base_url="http://127.0.0.1")
    assert c.get("/v1/me", headers={"Authorization": f"Bearer {t1}"}).json()["user"]["login"] == "Alice-Dev"
    # isolation: Alice's token cannot read Bob's workspace
    assert c.get(f"/v1/projects/{p2}", headers={"Authorization": f"Bearer {t1}"}).status_code == 403
    # users cannot change the server's shared Claude key or settings
    assert c.patch("/v1/integrations/claude", json={"model": "claude-haiku-5-5"}, headers={"Authorization": f"Bearer {t1}"}).status_code == 403
    assert c.post("/v1/integrations/claude/key", json={"api_key": "x" * 30}, headers={"Authorization": f"Bearer {t1}"}).status_code == 403
    # connect codes carry the server address and a token for the user's own workspace
    code = c.post("/v1/connector/code", json={"name": "laptop"}, headers={"Authorization": f"Bearer {t1}"}).json()["code"]
    import base64, json
    info = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))
    assert core.projects.authenticate(info["token"])["project_id"] == p1
    assert c.get("/v1/auth/config").json() == {"mode": "token", "github": False}


def test_daily_claude_cap_per_user(make_core, monkeypatch):
    core = make_core(CLAUDE_DAILY_CALLS_PER_PROJECT=2)
    core.projects.create_project("u_x", "x", "o")
    core.claude.bill_to("u_x")
    core.claude._charge()
    core.claude._charge()
    with pytest.raises(ClaudeLimitReached):
        core.claude._charge()
    core.claude.bill_to("u_y")
    core.claude._charge()  # other users are unaffected
