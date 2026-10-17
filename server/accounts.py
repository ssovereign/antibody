"""Hosted accounts: sign in with GitHub; every user gets their own project.

The first sign-in creates the user's project with the practice agents and documents ready (nothing run,
nothing learned), so each person can do the whole story themselves. Their computers connect with a
connect code (an access token for their project plus the server address), see scripts/antibody_connector.py.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
import urllib.parse
import urllib.request

from .canonical import new_id, utcnow
from .projects import InvalidRequest

_STATES: dict[str, float] = {}


class Accounts:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.cfg = core.config

    def github_enabled(self) -> bool:
        return bool(self.cfg.GITHUB_CLIENT_ID and self.cfg.GITHUB_CLIENT_SECRET and self.cfg.PUBLIC_URL)

    def callback_url(self) -> str:
        return self.cfg.PUBLIC_URL.rstrip("/") + "/v1/auth/github/callback"

    def github_start_url(self) -> str:
        if not self.github_enabled():
            raise InvalidRequest("GITHUB_NOT_CONFIGURED", "GitHub sign-in is not configured on this server.")
        now = time.time()
        for k, t in list(_STATES.items()):
            if t < now:
                _STATES.pop(k, None)
        state = secrets.token_urlsafe(24)
        _STATES[state] = now + 600
        q = urllib.parse.urlencode({"client_id": self.cfg.GITHUB_CLIENT_ID, "redirect_uri": self.callback_url(),
                                    "scope": "read:user", "state": state, "allow_signup": "true"})
        return f"https://github.com/login/oauth/authorize?{q}"

    def _http(self, url: str, data: dict | None = None, token: str | None = None) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "ANTIBODY"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = urllib.parse.urlencode(data).encode() if data else None
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def github_callback(self, code: str, state: str) -> str:
        """Returns an access token for the signed-in user's project."""
        if not state or _STATES.pop(state, 0) < time.time():
            raise InvalidRequest("SIGN_IN_EXPIRED", "That sign-in link expired. Please try again.")
        tok = self._http("https://github.com/login/oauth/access_token",
                         {"client_id": self.cfg.GITHUB_CLIENT_ID, "client_secret": self.cfg.GITHUB_CLIENT_SECRET,
                          "code": code, "redirect_uri": self.callback_url()})
        if not tok.get("access_token"):
            raise InvalidRequest("GITHUB_REFUSED", tok.get("error_description") or "GitHub did not accept the sign-in.")
        gh = self._http("https://api.github.com/user", token=tok["access_token"])
        return self.sign_in("github", str(gh["id"]), gh.get("login") or f"user{gh['id']}", gh.get("name"), gh.get("avatar_url"))

    def sign_in(self, provider: str, uid: str, login: str, name: str | None = None, avatar: str | None = None) -> str:
        now = utcnow()
        u = self.db.one("SELECT * FROM users WHERE provider=? AND provider_uid=?", (provider, uid))
        if not u:
            pid = self._new_project_id(login)
            from .demo_env import seed_fresh
            seed_fresh(self.core, pid, login)
            self.db.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?)",
                            (new_id("user"), provider, uid, login, name, avatar, pid, now, now))
            u = self.db.one("SELECT * FROM users WHERE provider=? AND provider_uid=?", (provider, uid))
        else:
            self.db.execute("UPDATE users SET last_login=?, login=?, name=?, avatar_url=? WHERE user_id=?", (now, login, name, avatar, u["user_id"]))
        return self.core.projects.issue_token(u["project_id"], "app", f"user:{login}")

    def _new_project_id(self, login: str) -> str:
        base = "u_" + (re.sub(r"[^a-z0-9]+", "_", login.lower()).strip("_")[:30] or "user")
        pid, n = base, 2
        while self.db.one("SELECT 1 FROM projects WHERE project_id=?", (pid,)):
            pid, n = f"{base}_{n}", n + 1
        return pid

    def user_for_project(self, project_id: str) -> dict | None:
        return self.db.one("SELECT login, name, avatar_url FROM users WHERE project_id=?", (project_id,))

    def connect_code(self, project_id: str, server: str, name: str) -> dict:
        token = self.core.projects.issue_token(project_id, "app", f"connector:{name}"[:60])
        code = base64.urlsafe_b64encode(json.dumps({"server": server.rstrip("/"), "token": token}).encode()).decode().rstrip("=")
        return {"code": code, "server": server.rstrip("/")}
