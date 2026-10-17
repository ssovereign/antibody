"""`antibody` - Git-style terminal commands for agent version history, plus chat.

Talks to the running ANTIBODY service (default http://127.0.0.1:8765) and
remembers your current project, agent, branch and conversation in
var/cli_state.json.

  antibody agents                    list agents
  antibody use "Claude assistant"    pick the agent to work with
  antibody status | log | save "msg" | diff [a] [b] | branch [name] | switch <b>
  antibody restore <ref> | merge <branch> [keys|all] | lessons | runs | effects
  antibody chat "summarise q3_notes" talk to the agent (Claude must be connected)
  antibody chat --new "Q3 work"      start a new conversation first
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "var" / "cli_state.json"
URL = os.environ.get("ANTIBODY_URL", "http://127.0.0.1:8765")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


class Client:
    def __init__(self, project: str | None):
        self.project = project
        self.token = None

    def _req(self, method: str, path: str, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            err = json.loads(e.read() or b"{}").get("error", {})
            raise SystemExit(f"antibody: {err.get('message') or e.reason} ({err.get('code', e.code)})")
        except urllib.error.URLError:
            raise SystemExit(f"antibody: cannot reach the service at {URL}. Start it with `make dev`.")

    def login(self):
        res = self._req("POST", "/v1/session/bootstrap", {"project_id": self.project} if self.project else {}, auth=False)
        self.token, self.project = res["token"], res["project_id"]
        return res

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body):
        return self._req("POST", path, body)


def main(argv: list[str]) -> None:
    st = load_state()
    args = list(argv)
    for flag in ("--project", "--agent"):
        if flag in args:
            i = args.index(flag)
            st[flag[2:]] = args[i + 1]
            del args[i:i + 2]
            if flag == "--agent":
                st.pop("agent_id", None)
                st.pop("branch_id", None)
    if not args or args[0] in ("-h", "--help", "help") and len(args) == 1:
        print(__doc__.strip())
        if args:
            args = ["help"]
        else:
            return
    c = Client(st.get("project"))
    c.login()
    st["project"] = c.project
    cmd = args[0]

    if cmd == "agents":
        for a in c.get(f"/v1/agents?project_id={c.project}"):
            mark = "*" if a["agent_id"] == st.get("agent_id") else " "
            print(f"{mark} {a['name']:<30} {a['adapter']:<20} {a['agent_id']}")
        return
    if cmd == "use":
        if len(args) < 2:
            raise SystemExit("usage: antibody use <agent name>")
        name = " ".join(args[1:])
        match = [a for a in c.get(f"/v1/agents?project_id={c.project}") if a["name"] == name or a["agent_id"] == name]
        if not match:
            raise SystemExit(f"antibody: no agent called {name}. Try `antibody agents`.")
        st.update(agent=match[0]["name"], agent_id=match[0]["agent_id"], branch_id=match[0]["accepted_branch_id"])
        st.pop("conversation_id", None)
        save_state(st)
        print(f"Using {match[0]['name']} on branch main.")
        return
    if cmd == "chat":
        return chat(c, st, args[1:])

    if not st.get("agent_id"):
        agents = [a for a in c.get(f"/v1/agents?project_id={c.project}") if a["name"] == st.get("agent")]
        if not agents:
            raise SystemExit("antibody: pick an agent first, e.g. `antibody use \"Report assistant A\"` (see `antibody agents`).")
        st.update(agent_id=agents[0]["agent_id"], branch_id=agents[0]["accepted_branch_id"])
    res = c.post("/v1/commands", {"project_id": c.project, "agent_id": st["agent_id"], "branch_id": st.get("branch_id"),
                                  "command": " ".join(_quote(a) for a in args)})
    print(res["output"])
    if res.get("branch_id"):
        st["branch_id"] = res["branch_id"]
    save_state(st)
    if not res.get("ok"):
        sys.exit(1)


def _quote(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a


def chat(c: Client, st: dict, args: list[str]) -> None:
    if args[:1] == ["--new"]:
        title = args[1] if len(args) > 1 else "Terminal conversation"
        conv = c.post("/v1/chats", {"project_id": c.project, "title": title, "agent_id": st.get("agent_id")})
        st.update(conversation_id=conv["conversation_id"], agent_id=conv["agent_id"], agent=conv["agent"]["name"], branch_id=conv["branch_id"])
        save_state(st)
        print(f"Started conversation {conv['conversation_id']} with {conv['agent']['name']}.")
        args = args[2:]
        if not args:
            return
    if not st.get("conversation_id"):
        raise SystemExit("antibody: no conversation yet. Start one with `antibody chat --new \"title\" \"your message\"`.")
    text = " ".join(args)
    if not text:
        for m in c.get(f"/v1/chats/{st['conversation_id']}/messages")["messages"][-10:]:
            print(f"[{m['role']}] {m['text']}" + (f"\n{m['meta']['output']}" if m["role"] == "command" else ""))
        return
    res = c.post(f"/v1/chats/{st['conversation_id']}/messages", {"text": text})
    st["branch_id"] = res["conversation"]["branch_id"]
    save_state(st)
    if res.get("command"):
        print(res["command"]["output"])
        return
    seq = res["message"]["seq"]
    print("…waiting for the reply (every tool call is checked by ANTIBODY)")
    while True:
        time.sleep(1.5)
        msgs = c.get(f"/v1/chats/{st['conversation_id']}/messages?after={seq}")["messages"]
        done = [m for m in msgs if m["role"] in ("assistant", "system") and m.get("run_id") == res["run"]["run_id"]]
        if done:
            m = done[-1]
            print(m["text"])
            if m["meta"].get("blocked"):
                print(f"({m['meta']['blocked']} action(s) were blocked; see run {m['run_id']} in the web UI)")
            return


if __name__ == "__main__":
    main(sys.argv[1:])
