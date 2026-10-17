"""Connection to Claude (Anthropic API).

Credentials are resolved in this order: a key saved through Settings (kept
server-side in var/secrets.json, never returned to the browser), then the
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN environment variables. The base URL
is set explicitly (default https://api.anthropic.com) so a key is never sent
to an endpoint inherited from some other tool's environment.

Defaults follow current guidance: model claude-opus-5-5, adaptive thinking
(always on for that model), effort "medium", and server-side refusal
fallbacks ("default" routing) enabled.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .canonical import utcnow

DEFAULTS = {
    "model": "claude-opus-5-5",
    "effort": "medium",
    "base_url": "https://api.anthropic.com",
    "fallbacks": True,
    "auditor": "rules",  # rules | claude
}
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeNotConnected(Exception):
    pass


class ClaudeConnection:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self.http_client = None  # test hook: an anthropic.DefaultHttpxClient with a mock transport
        self._settings_path = Path(config.SECRETS_PATH).with_name("integrations.json")

    # ------------------------------------------------------------ storage
    def _secrets(self) -> dict:
        p = Path(self.config.SECRETS_PATH)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _write_secrets(self, data: dict) -> None:
        p = Path(self.config.SECRETS_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def settings(self) -> dict:
        s = dict(DEFAULTS)
        if self._settings_path.exists():
            s.update(json.loads(self._settings_path.read_text(encoding="utf-8")).get("claude", {}))
        return s

    def update_settings(self, changes: dict) -> dict:
        allowed = {"model", "effort", "base_url", "fallbacks", "auditor"}
        bad = set(changes) - allowed
        if bad:
            raise ValueError(f"not editable: {sorted(bad)}")
        if "model" in changes and changes["model"] not in MODELS:
            raise ValueError(f"model must be one of {MODELS}")
        if "effort" in changes and changes["effort"] not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        if "base_url" in changes and not str(changes["base_url"]).startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("base_url must be https (or a loopback address for a local gateway)")
        if "auditor" in changes and changes["auditor"] not in ("rules", "claude"):
            raise ValueError("auditor must be rules or claude")
        with self._lock:
            data = json.loads(self._settings_path.read_text(encoding="utf-8")) if self._settings_path.exists() else {}
            data["claude"] = dict(data.get("claude", {}), **changes)
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return self.settings()

    def save_key(self, key: str) -> None:
        key = (key or "").strip()
        if len(key) < 20 or " " in key:
            raise ValueError("that does not look like an Anthropic API key")
        with self._lock:
            s = self._secrets()
            s["anthropic_api_key"] = key
            self._write_secrets(s)

    def clear_key(self) -> None:
        with self._lock:
            s = self._secrets()
            s.pop("anthropic_api_key", None)
            self._write_secrets(s)

    def _credential(self) -> tuple[str | None, str | None, str | None]:
        """(api_key, auth_token, source)."""
        saved = self._secrets().get("anthropic_api_key")
        if saved:
            return saved, None, "saved in ANTIBODY settings"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return os.environ["ANTHROPIC_API_KEY"], None, "ANTHROPIC_API_KEY environment variable"
        if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return None, os.environ["ANTHROPIC_AUTH_TOKEN"], "ANTHROPIC_AUTH_TOKEN environment variable"
        return None, None, None

    def connected(self) -> bool:
        return self._credential()[2] is not None or self.http_client is not None

    def status(self) -> dict:
        key, token, source = self._credential()
        cred = key or token
        st = self.settings()
        last = {}
        if self._settings_path.exists():
            last = json.loads(self._settings_path.read_text(encoding="utf-8")).get("claude_last_test", {})
        return {"connected": bool(source) or self.http_client is not None, "source": source,
                "key_hint": f"…{cred[-4:]}" if cred else None, "settings": st, "models": MODELS, "efforts": EFFORTS,
                "last_test": last}

    # ------------------------------------------------------------ calls
    def client(self):
        import anthropic
        key, token, source = self._credential()
        if not source and self.http_client is None:
            raise ClaudeNotConnected("Claude is not connected. Add an Anthropic API key in Settings → Connect Claude.")
        kw = {"base_url": self.settings()["base_url"], "max_retries": 2, "timeout": 600.0}
        if key:
            kw["api_key"] = key
        elif token:
            kw["auth_token"] = token
        else:
            kw["api_key"] = "test-key"
        if self.http_client is not None:
            kw["http_client"] = self.http_client
        return anthropic.Anthropic(**kw)

    def create(self, **kwargs):
        """messages.create with ANTIBODY's defaults (effort, refusal fallbacks)."""
        st = self.settings()
        kwargs.setdefault("model", st["model"])
        kwargs.setdefault("max_tokens", 16000)
        oc = dict(kwargs.pop("output_config", {}) or {})
        oc.setdefault("effort", st["effort"])
        kwargs["output_config"] = oc
        c = self.client()
        if st.get("fallbacks") and kwargs["model"] in ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"):
            return c.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return c.messages.create(**kwargs)

    def test(self) -> dict:
        import anthropic
        try:
            r = self.create(max_tokens=64, output_config={"effort": "low"},
                            messages=[{"role": "user", "content": "Reply with the single word: connected"}])
            text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text").strip()
            res = {"ok": True, "model": r.model, "reply": text[:80], "at": utcnow()}
        except ClaudeNotConnected as e:
            res = {"ok": False, "error": str(e), "at": utcnow()}
        except anthropic.AuthenticationError:
            res = {"ok": False, "error": "The API key was rejected (authentication failed).", "at": utcnow()}
        except anthropic.PermissionDeniedError:
            res = {"ok": False, "error": "The key lacks permission for this model.", "at": utcnow()}
        except anthropic.NotFoundError:
            res = {"ok": False, "error": "Model or endpoint not found. Check the model and base URL.", "at": utcnow()}
        except anthropic.RateLimitError:
            res = {"ok": False, "error": "Rate limited. Try again shortly.", "at": utcnow()}
        except anthropic.APIStatusError as e:
            res = {"ok": False, "error": f"API error {e.status_code}: {str(e.message)[:200]}", "at": utcnow()}
        except anthropic.APIConnectionError:
            res = {"ok": False, "error": "Could not reach the API. Check the network and base URL.", "at": utcnow()}
        with self._lock:
            data = json.loads(self._settings_path.read_text(encoding="utf-8")) if self._settings_path.exists() else {}
            data["claude_last_test"] = res
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return res
