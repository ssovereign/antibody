# Getting someone started with ANTIBODY

## Just looking (hosted demo)

If you run a hosted server (see `HOSTING.md`), send them the URL and an access token:

```bash
python -m server.cli invite --project demo_stage --name <their name>
```

They open the URL, paste the token, and can run the practice agents and the web Chat.

## Using it with their own Claude Code (local, about 10 minutes)

**Needs:** Python 3.12, Node 18+ and Claude Code (the desktop app, or the `claude` CLI).

1. **Get the code and set it up** (creates a virtual environment, installs everything, builds the website):
   ```bash
   python scripts/setup.py
   ```
2. **Start ANTIBODY** and leave it running:
   ```bash
   python scripts/dev.py
   ```
   Then open http://127.0.0.1:8765. No sign-in is needed on your own machine.
3. **Connect Claude (optional, recommended).** In Settings → Connect Claude, paste an API key from console.anthropic.com. Use a key created inside a workspace; for an organization-level key, also fill in the workspace ID. Click **Test connection**. This powers Chat, the Claude safety auditor and Claude-written lessons.
4. **Make a practice environment:**
   ```bash
   .venv/Scripts/python.exe -m server.cli demo-env --name practice
   ```
   (On macOS/Linux: `.venv/bin/python`.) This creates `~/Documents/antibody-demo-practice` with a small app, sample documents and the guard installed, plus a matching fresh project on the website.
5. **Open that folder in Claude Code**, in the desktop app's Code tab or with `claude` in a terminal there, and type `ab: status`. ANTIBODY should answer. Then follow the folder's `README.md`.
6. **For merges and restoring saved states from a terminal**, sign the Claude Code CLI in once: run `claude`, then `/login`.

**To guard a real project:** Settings → Guard Claude Code in a project folder, or:
```bash
.venv/Scripts/python.exe -m server.cli claude-code-install --project-dir <folder>
```

**To remove the guard** from a folder:
```bash
.venv/Scripts/python.exe -m server.cli claude-code-install --project-dir <folder> --uninstall
```

## Fresh, identical demo environments

```bash
.venv/Scripts/python.exe -m server.cli demo-env --name stage --name rehearsal
```

Each name gets its own website project (`demo_<name>`, with practice agents ready and nothing learned) and its own guarded folder. Add `--reset` to rebuild one exactly as new before each rehearsal.

## What each screen is for

- **Overview, Runs, Incidents, Knowledge, Evaluation:** what agents did, what was blocked, what was learned, and how lessons were tested.
- **Chat:** talk to Claude with ANTIBODY checking every action. `/save`, `/log` and `/restore` work there.
- **Claude Code history:** saved states of each guarded Claude Code folder (files, conversation and memory). Go back, compare, negotiated merge, and lessons learned from failed commands.
- **Version history:** the same idea for ANTIBODY's own agents (practice agents and Chat). Their state is instructions, lessons, memory facts and work files, with a key-by-key merge.
- **Effects & outbox:** every outside action, and how to undo what can be undone.
- **Settings:** the Claude connection, the guard installer, the safety auditor and capability profiles.
