"""Git-style text commands for an agent's version history (RECEIPT).

The same commands work in chat (typed with a leading "/") and in the terminal
(`antibody <command>`). Each call names an agent and its current branch; a
command that moves you to another branch returns the new branch_id.

References accepted wherever a version is expected:
  HEAD, HEAD~2         the current branch head and its first-parent ancestors
  <branch name>        that branch's head
  <id prefix>          a version id prefix (6+ hex characters), with or without "sha256:"
  "<label>"            a version's exact label
"""
from __future__ import annotations

import shlex

from .canonical import dumps, loads
from .projects import ConflictError, InvalidRequest, NotFound

HELP = """Version history commands (like Git, for this agent):
  status                      where you are: branch, saved version, unsaved changes
  log [n]                     recent saved versions on this branch
  save [message]              save the current state as a version
  diff [a] [b]                compare two versions (default: last save vs now)
  branch                      list branches
  branch <name> [from <ref>]  start a new branch and switch to it
  switch <branch>             move to another branch
  restore <ref> [name]        bring an old version back on a new branch
  merge <branch|ref> [keys|all]  keep selected changes from another branch
  remember <key> <value>      set a memory fact on this branch
  forget <key>                remove a memory fact on this branch
  lessons                     accepted lessons and patterns this agent can use
  disable <lesson@rev>        stop using a lesson on this branch only
  revoke <lesson> <reason>    withdraw a lesson everywhere (all agents)
  runs [n]                    recent runs on this branch
  effects [run]               what a run did outside the agent (default: last run)
For Claude Code projects (typed as 'ab: <command>' in Claude Code):
  A checkpoint (files + the exact conversation) is saved before every prompt you send.
  rewind [n]                  go back n prompts: files and agent state as they were (default 1)
  undo                        same as 'rewind 1'
  restore / switch / rewind   write that version's files into the folder (like git checkout) and
                              open its exact conversation as a new Claude Code session to resume
  merge <branch>              negotiated merge: both agents vote on every change from their own
                              memory, a third agent arbitrates what they still disagree on
  tree                        all branches and where they came from
  transcript [ref]            read the conversation saved with a version
Refs: HEAD, HEAD~1, a branch name, a version id prefix, or a quoted label."""


class Commands:
    def __init__(self, core):
        self.core = core
        self.db = core.db
        self.v = core.versions

    # ------------------------------------------------------------ helpers
    def _branch_by_name(self, agent_id: str, name: str) -> dict | None:
        r = self.db.one("SELECT branch_id FROM branches WHERE agent_id=? AND name=?", (agent_id, name))
        if not r:
            r = self.db.one("SELECT branch_id FROM branches WHERE agent_id=? AND branch_id=?", (agent_id, name))
        return self.v.branch(r["branch_id"]) if r else None

    def resolve(self, project_id: str, agent_id: str, branch: dict, ref: str) -> str:
        ref = ref.strip()
        if ref.upper().startswith("HEAD"):
            n = 0
            if "~" in ref:
                try:
                    n = int(ref.split("~", 1)[1] or 1)
                except ValueError:
                    raise CommandError(f"cannot read '{ref}'")
            vid = branch["head_version"]
            for _ in range(n):
                if not vid:
                    break
                parents = loads(self.db.scalar("SELECT parents_json FROM versions WHERE version_id=?", (vid,)), [])
                vid = parents[0] if parents else None
            if not vid:
                raise CommandError(f"{ref} does not exist on this branch")
            return vid
        b = self._branch_by_name(agent_id, ref)
        if b:
            if not b["head_version"]:
                raise CommandError(f"branch {ref} has no saved version yet")
            return b["head_version"]
        rows = self.db.all("SELECT v.version_id FROM versions v JOIN branches b ON b.branch_id=v.branch_id WHERE v.project_id=? AND b.agent_id=? AND v.label=?",
                           (project_id, agent_id, ref))
        if len(rows) == 1:
            return rows[0]["version_id"]
        prefix = ref[7:] if ref.startswith("sha256:") else ref
        if len(prefix) >= 6 and all(c in "0123456789abcdef" for c in prefix.lower()):
            rows = self.db.all("SELECT v.version_id FROM versions v JOIN branches b ON b.branch_id=v.branch_id WHERE v.project_id=? AND b.agent_id=? AND v.version_id LIKE ?",
                               (project_id, agent_id, f"sha256:{prefix.lower()}%"))
            if len(rows) == 1:
                return rows[0]["version_id"]
            if len(rows) > 1:
                raise CommandError(f"'{ref}' matches {len(rows)} versions; use more characters")
        raise CommandError(f"no version, branch or label called '{ref}'")

    @staticmethod
    def sid(vid: str | None) -> str:
        return vid[7:17] if vid else "(none)"

    def _label(self, vid: str) -> str:
        return self.db.scalar("SELECT label FROM versions WHERE version_id=?", (vid,)) or ""

    # ------------------------------------------------------------ entry
    def run(self, project_id: str, agent_id: str, branch_id: str | None, line: str, actor: str) -> dict:
        agent = self.core.coordinator.agent(agent_id)
        if agent["project_id"] != project_id:
            raise NotFound(f"agent {agent_id}")
        branch = self.v.branch(branch_id or agent["accepted_branch_id"])
        if branch["agent_id"] != agent_id:
            raise InvalidRequest("BRANCH_MISMATCH", "that branch belongs to another agent")
        line = line.strip().lstrip("/")
        try:
            parts = shlex.split(line)
        except ValueError as e:
            return {"ok": False, "output": f"Could not read the command: {e}", "branch_id": branch["branch_id"]}
        if not parts:
            parts = ["help"]
        cmd, args = parts[0].lower(), parts[1:]
        handler = getattr(self, f"cmd_{cmd.replace('-', '_')}", None)
        aliases = {"checkout": self.cmd_switch, "commit": self.cmd_save, "history": self.cmd_log, "rollback": self.cmd_restore,
                   "checkpoints": self.cmd_log}
        handler = handler or aliases.get(cmd)
        if handler is None:
            return {"ok": False, "output": f"Unknown command '{cmd}'. Type 'help' for the list.", "branch_id": branch["branch_id"]}
        host = self.core.claude_code if agent["adapter"] == "claude_code_hooks" else None
        moving = bool(host) and (cmd in ("switch", "checkout", "restore", "rollback", "rewind", "undo") or (cmd == "merge" and args))
        before = branch["head_version"]
        try:
            if host:
                # the project folder is this branch's working tree
                host.sync_workspace(agent, branch, include_transcript=cmd in ("save", "commit"))
                branch = self.v.branch(branch["branch_id"])
                if moving and before and not self.v.is_clean(branch["branch_id"]):
                    # never lose work: checkpoint the current files and conversation first
                    host.checkpoint(agent, None, f"auto: before {cmd}", actor)
                    branch = self.v.branch(branch["branch_id"])
                    before = branch["head_version"]
            out = handler(project_id, agent, branch, args, actor)
        except CommandError as e:
            return {"ok": False, "output": str(e), "branch_id": branch["branch_id"]}
        except ConflictError as e:
            extra = ""
            if e.code == "MERGE_CONFLICT":
                extra = "\n" + "\n".join(f"  conflict: {c['key']} ({c['kind'].replace('_', ' ')})" for c in (e.detail or {}).get("conflicts", []))
                extra += "\nBoth sides changed these. Merge them one at a time, or keep one side in the Version history screen."
            return {"ok": False, "output": f"{e.message}{extra}", "branch_id": branch["branch_id"]}
        except (InvalidRequest, NotFound) as e:
            return {"ok": False, "output": e.message, "branch_id": branch["branch_id"]}
        if isinstance(out, str):
            out = {"output": out}
        out.setdefault("branch_id", branch["branch_id"])
        out.setdefault("ok", True)
        if host:
            new = self.v.branch(out["branch_id"])
            data = out.get("data") or {}
            moved = new["head_version"] != before or out["branch_id"] != branch["branch_id"]
            if moving and new["head_version"] and moved and not data.get("checked_out"):
                changes = host.checkout(agent, before, new["head_version"])
                lines = [f"Project folder updated: {len(changes)} file change(s)."] + [f"  {c}" for c in changes[:25]]
                if len(changes) > 25:
                    lines.append("  ...")
                opened = host.open_state(agent, new["head_version"], f"ANTIBODY: {new['name']} @ {self.sid(new['head_version'])}")
                if opened:
                    lines.append(f"Agent state ({opened['messages']} conversation entries) is ready as a new Claude Code session:")
                    lines.append(f"  claude --resume {opened['session_id']}")
                    lines.append("Resume it to continue exactly where that agent was. This session keeps its own history.")
                    if opened["removed"]:
                        lines.append(f"  ANTIBODY removed {len(opened['removed'])} quarantined instruction(s) from the restored conversation.")
                else:
                    lines.append("That version has no saved conversation (files only).")
                out["output"] += "\n" + "\n".join(lines)
                out.setdefault("data", {})["session"] = opened
            if out["branch_id"] != agent["config"].get("current_branch"):
                cfg = dict(self.core.coordinator.agent(agent["agent_id"])["config"], current_branch=out["branch_id"])
                self.db.execute("UPDATE agents SET config_json=? WHERE agent_id=?", (dumps(cfg), agent["agent_id"]))
        return out

    def cmd_rewind(self, pid, agent, branch, args, actor):
        if agent["adapter"] != "claude_code_hooks":
            raise CommandError("rewind is for Claude Code projects; use 'restore HEAD~1' here")
        n = int(args[0]) if args and args[0].isdigit() else 1
        vid = self.resolve(pid, agent["agent_id"], branch, f"HEAD~{n}")
        f = self.v.fork(pid, vid, None, "restore", actor)
        return {"output": f"Rewound {n} prompt(s) to {self.sid(vid)} {self._label(vid)} on new branch {f['branch']['name']} "
                          f"(the newer work stays on {branch['name']}).", "branch_id": f["branch"]["branch_id"]}

    def cmd_undo(self, pid, agent, branch, args, actor):
        return self.cmd_rewind(pid, agent, branch, ["1"], actor)

    def cmd_tree(self, pid, agent, branch, args, actor):
        rows = self.v.branches(pid, agent["agent_id"])
        by_parent: dict = {}
        for b in rows:
            by_parent.setdefault(b["parent_branch_id"], []).append(b)
        lines = []

        def walk(parent, depth):
            for b in by_parent.get(parent, []):
                n = self.db.scalar("SELECT COUNT(*) FROM versions WHERE branch_id=?", (b["branch_id"],))
                mark = "*" if b["branch_id"] == branch["branch_id"] else " "
                fork = f" from {self.sid(b['base_version'])}" if b["base_version"] and b["parent_branch_id"] else ""
                lines.append(f"{mark} {'  ' * depth}{b['name']}{fork}: {n} version(s), head {self.sid(b['head_version'])} "
                             f"{self._label(b['head_version']) if b['head_version'] else ''}".rstrip())
                walk(b["branch_id"], depth + 1)
        walk(None, 0)
        return "\n".join(lines) or "No branches."

    def cmd_transcript(self, pid, agent, branch, args, actor):
        if agent["adapter"] != "claude_code_hooks":
            raise CommandError("transcript is for Claude Code projects")
        vid = self.resolve(pid, agent["agent_id"], branch, args[0] if args else "HEAD")
        return f"Conversation saved with {self.sid(vid)} {self._label(vid)}:\n" + self.core.claude_code.transcript(pid, vid)

    # ------------------------------------------------------------ commands
    def cmd_help(self, pid, agent, branch, args, actor):
        return HELP

    def cmd_status(self, pid, agent, branch, args, actor):
        head = branch["head_version"]
        clean = self.v.is_clean(branch["branch_id"]) if head else False
        st = branch["state"]
        last = self.db.one("SELECT run_id, status, status_reason FROM runs WHERE branch_id=? ORDER BY created_at DESC LIMIT 1", (branch["branch_id"],))
        lines = [f"Agent: {agent['name']}",
                 f"On branch: {branch['name']} ({branch['purpose']})",
                 f"Last saved version: {self.sid(head)} {self._label(head) if head else ''}".rstrip(),
                 "Unsaved changes: none" if clean else "Unsaved changes: yes (use 'save' to keep them, 'diff' to see them)",
                 f"Lessons pinned here: {', '.join(st.get('lesson_revisions', [])) or 'none'}",
                 f"Memory facts: {', '.join(st.get('memory_facts', {})) or 'none'}"]
        if last:
            lines.append(f"Last run: {last['run_id']} {last['status']} ({last['status_reason'] or ''})")
        return "\n".join(lines)

    def cmd_log(self, pid, agent, branch, args, actor):
        n = int(args[0]) if args and args[0].isdigit() else 10
        vid, lines = branch["head_version"], []
        while vid and len(lines) < n:
            v = self.db.one("SELECT * FROM versions WHERE version_id=?", (vid,))
            parents = loads(v["parents_json"], [])
            tag = " (merge)" if len(parents) > 1 else ""
            here = " <- HEAD" if not lines else ""
            num = f"~{len(lines):<3} " if agent["adapter"] == "claude_code_hooks" else ""
            lines.append(f"{num}{self.sid(vid)}  {v['created_at'][:19].replace('T', ' ')}  {v['label'] or '(no label)'}{tag}{here}")
            vid = parents[0] if parents else None
        return "\n".join(lines) or "No saved versions on this branch yet."

    def cmd_save(self, pid, agent, branch, args, actor):
        label = " ".join(args).strip() or None
        if branch["head_version"] and self.v.is_clean(branch["branch_id"]) and not label:
            return f"Nothing to save: no changes since {self.sid(branch['head_version'])}."
        v = self.v.save(pid, branch["branch_id"], None, label, actor)
        return {"output": f"Saved {self.sid(v['version_id'])}" + (f' "{label}"' if label else "") + f" on {branch['name']}.",
                "data": {"version_id": v["version_id"]}}

    def cmd_diff(self, pid, agent, branch, args, actor):
        from .versions.store import diff_states
        if len(args) >= 2:
            a, b = self.resolve(pid, agent["agent_id"], branch, args[0]), self.resolve(pid, agent["agent_id"], branch, args[1])
            d = self.v.compare(pid, a, b)
            title = f"{self.sid(a)} -> {self.sid(b)}"
        else:
            if not branch["head_version"]:
                raise CommandError("nothing saved yet to compare against")
            a = self.resolve(pid, agent["agent_id"], branch, args[0]) if args else branch["head_version"]
            sa = self.v.manifest_state(pid, self.v.version(a)["manifest"])
            d = diff_states(self.v, pid, sa, self.v.working_state(branch["branch_id"], store=True))  # text diffs read the blobs
            title = f"{self.sid(a)} -> current (unsaved)"
        conv = [x for x in d["summary"] if "_claude_session/" in x["text"]]
        d["summary"] = [x for x in d["summary"] if "_claude_session/" not in x["text"]]
        if conv:
            d["summary"].append({"text": "conversation changed (saved with the version)"})
        d["changes"]["files"] = [f for f in d["changes"].get("files", []) if not f.get("path", "").startswith("_claude_session/")]
        d["change_keys"] = [k for k in d.get("change_keys", []) if "_claude_session/" not in k]
        if not d["summary"]:
            return f"No differences ({title})."
        lines = [f"Changes {title}:"] + [f"  {s['text']}" for s in d["summary"]]
        for f in d["changes"].get("files", [])[:3]:
            if f.get("text_diff"):
                lines.append(f"\n--- {f['path']}")
                lines += [l for l in f["text_diff"][2:42]]
        if d.get("change_keys"):
            lines.append("\nChange keys (for merge): " + ", ".join(d["change_keys"]))
        return "\n".join(lines)

    def cmd_branch(self, pid, agent, branch, args, actor):
        if not args:
            rows = self.v.branches(pid, agent["agent_id"])
            return "\n".join(f"{'*' if b['branch_id'] == branch['branch_id'] else ' '} {b['name']:<24} {b['purpose']:<10} head {self.sid(b['head_version'])}"
                             for b in rows)
        name = args[0]
        if self._branch_by_name(agent["agent_id"], name):
            raise CommandError(f"a branch called {name} already exists; use 'switch {name}'")
        ref = args[2] if len(args) >= 3 and args[1].lower() == "from" else "HEAD"
        vid = self.resolve(pid, agent["agent_id"], branch, ref)
        warn = ""
        if ref == "HEAD" and not self.v.is_clean(branch["branch_id"]):
            warn = "\nNote: unsaved changes on the old branch were not carried over (save them first if you want them)."
        f = self.v.fork(pid, vid, name, "experiment", actor)
        return {"output": f"Created branch {f['branch']['name']} from {self.sid(vid)} and switched to it.{warn}", "branch_id": f["branch"]["branch_id"]}

    def cmd_switch(self, pid, agent, branch, args, actor):
        if not args:
            raise CommandError("which branch? (type 'branch' to list them)")
        b = self._branch_by_name(agent["agent_id"], args[0])
        if not b:
            raise CommandError(f"no branch called {args[0]}")
        note = "" if self.v.is_clean(branch["branch_id"]) or not branch["head_version"] else f"\nNote: {branch['name']} still has unsaved changes; they stay there."
        return {"output": f"Switched to {b['name']} (head {self.sid(b['head_version'])}).{note}", "branch_id": b["branch_id"]}

    def cmd_restore(self, pid, agent, branch, args, actor):
        if not args:
            raise CommandError("restore which version? e.g. 'restore HEAD~1'")
        vid = self.resolve(pid, agent["agent_id"], branch, args[0])
        name = args[1] if len(args) > 1 else None
        f = self.v.fork(pid, vid, name, "restore", actor)
        lines = [f"Restored {self.sid(vid)} {self._label(vid)} into new branch {f['branch']['name']} and switched to it.",
                 "Completed outside actions (sent messages, payments) stay recorded and are not undone."]
        if f["excluded_knowledge"]:
            lines.append("Left out because they were revoked: " + ", ".join(x["ref"] for x in f["excluded_knowledge"]))
        if f["policy"]["differences"]:
            lines.append("Current rules differ from then: " + ", ".join(x["field"] for x in f["policy"]["differences"]) + " (current rules apply).")
        if f["pending_effects"]:
            lines.append(f"{len(f['pending_effects'])} action(s) still need reconciling before continuing.")
        return {"output": "\n".join(lines), "branch_id": f["branch"]["branch_id"]}

    def cmd_merge(self, pid, agent, branch, args, actor):
        if not args:
            raise CommandError("merge from which branch or version?")
        src = self.resolve(pid, agent["agent_id"], branch, args[0])
        if agent["adapter"] == "claude_code_hooks":
            return self.core.claude_code.merge(pid, agent, branch, src, args[0], actor)
        if not branch["head_version"]:
            raise CommandError("save this branch first")
        if not self.v.is_clean(branch["branch_id"]):
            raise CommandError("this branch has unsaved changes; 'save' first")
        keys = self.v.compare(pid, branch["head_version"], src)["change_keys"]
        if not keys:
            return "Nothing to merge: no differences."
        if len(args) < 2:
            return ("Pick what to keep. Changes available:\n  " + "\n  ".join(keys) +
                    f"\nExample: merge {args[0]} {keys[0]}   (or: merge {args[0]} all)")
        sel = keys if args[1].lower() == "all" else [k for part in args[1:] for k in part.split(",") if k]
        unknown = [k for k in sel if k not in keys]
        if unknown:
            raise CommandError("not a change between these versions: " + ", ".join(unknown))
        res = self.core.merges.merge(pid, branch["branch_id"], branch["head_version"], src, sel, {}, None, actor)
        applied = "\n".join(f"  {a['key']}: {a['result'].replace('_', ' ')}" for a in res["applied"])
        return {"output": f"Merged into {branch['name']} as {self.sid(res['version']['version_id'])} after combined checks passed "
                          f"(evaluation {res['evaluation_id']}).\n{applied}", "data": {"version_id": res["version"]["version_id"]}}

    def cmd_remember(self, pid, agent, branch, args, actor):
        if len(args) < 2:
            raise CommandError("usage: remember <key> <value>")
        self.v.edit_memory(branch["branch_id"], args[0], " ".join(args[1:]), origin="USER_ASSIGNMENT")
        return f"Remembered {args[0]} on {branch['name']} (unsaved; 'save' to keep a version)."

    def cmd_forget(self, pid, agent, branch, args, actor):
        if not args:
            raise CommandError("usage: forget <key>")
        if args[0] not in branch["state"].get("memory_facts", {}):
            raise CommandError(f"no memory fact called {args[0]} on this branch")
        self.v.edit_memory(branch["branch_id"], args[0], delete=True)
        return f"Forgot {args[0]} on {branch['name']} (unsaved; 'save' to keep a version)."

    def cmd_lessons(self, pid, agent, branch, args, actor):
        active = self.core.registry.active(pid)
        if not active:
            return "No accepted lessons or patterns yet."
        disabled = set(branch["state"].get("disabled_lessons", []))
        return "\n".join(f"{l['ref']:<40} {l['kind']:<15} {'(disabled here) ' if l['ref'] in disabled else ''}{l['body'].get('summary', '')[:90]}"
                         for l in active)

    def cmd_disable(self, pid, agent, branch, args, actor):
        if not args or "@" not in args[0]:
            raise CommandError("usage: disable <lesson@revision>")
        self.core.learning.disable_on_branch(branch["branch_id"], args[0])
        return f"{args[0]} will not be used on {branch['name']}. Other branches and agents are unaffected."

    def cmd_revoke(self, pid, agent, branch, args, actor):
        if len(args) < 2:
            raise CommandError("usage: revoke <lesson[@revision]> <reason>")
        lid, _, rev = args[0].partition("@")
        res = self.core.learning.revoke(pid, lid, int(rev) if rev.isdigit() else None, " ".join(args[1:]), actor)
        return f"Revoked {args[0]} for every agent (registry revision {res['registry_revision']}). Restoring old versions will not bring it back."

    def cmd_runs(self, pid, agent, branch, args, actor):
        n = int(args[0]) if args and args[0].isdigit() else 8
        rows = self.db.all("SELECT run_id, task_id, status, status_reason, mode, created_at FROM runs WHERE branch_id=? ORDER BY created_at DESC LIMIT ?",
                           (branch["branch_id"], n))
        return "\n".join(f"{r['run_id']}  {r['created_at'][:16].replace('T', ' ')}  {r['status']:<10} {r['status_reason'] or '':<20} {r['mode']}"
                         for r in rows) or "No runs on this branch yet."

    def cmd_effects(self, pid, agent, branch, args, actor):
        rid = args[0] if args else self.db.scalar("SELECT run_id FROM runs WHERE branch_id=? ORDER BY created_at DESC LIMIT 1", (branch["branch_id"],))
        if not rid:
            return "No runs on this branch yet."
        imp = self.core.effects.impact(pid, rid)
        c = imp["counts"]
        lines = [f"Run {rid}: {c['messages_sent']} message(s) sent, {c['payments']} payment(s), {c['file_changes']} file change(s), "
                 f"{c['blocked']} blocked, {c['unknown']} unknown."]
        lines += [f"- {m['text']}" for m in imp["mitigation"]]
        return "\n".join(lines)


class CommandError(Exception):
    pass
