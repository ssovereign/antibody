# Hosting ANTIBODY

ANTIBODY has two parts that host differently.

| Part | What it does | Where it can run |
|---|---|---|
| **Server** (`server/`) | Gateway, learning, history, chat, workers. Keeps a SQLite database and files on disk, runs background jobs, holds long requests (a negotiated merge takes minutes), and streams live events | A machine or container with a **persistent disk**: your laptop, Render, Railway, Fly.io, any VM. **Not Vercel**: Vercel functions are short-lived, have no persistent disk and can't run background workers |
| **Website** (`apps/web/`) | The dashboard and Chat UI. Static files | Vercel (or any static host), or served by the server itself |

## What a hosted server can and cannot do

- **Works hosted:** the website, the practice agents and the full learning story, web Chat with Claude, invites.
- **Stays local:** the **Claude Code guard and its history**. ANTIBODY has to read and write each person's project folder and Claude Code's session files on *their* computer, so each person runs ANTIBODY on their own machine for that part (see `ONBOARDING.md`). A hosted server is for the shared dashboard and demo.

## Option A: one container (simplest)

The `Dockerfile` builds the website and the server into one image. All state lives on `/data`, people sign in with access tokens, and a fresh demo project `demo_stage` is created on first start.

```bash
docker build -t antibody .
```
```bash
docker run -p 8765:8765 -v antibody-data:/data -e ANTHROPIC_API_KEY=sk-ant-... antibody
```

**Render:** New → Blueprint → this repository. It uses `render.yaml`. Set `ANTHROPIC_API_KEY` in the dashboard, and change `EXTRA_ALLOWED_HOSTS` to your service's host name.

**Railway / Fly.io:** deploy the Dockerfile, mount a volume at `/data`, and set the same environment variables.

## Option B: website on Vercel, server elsewhere

1. Deploy the server as in option A, and note its URL (for example `https://antibody.onrender.com`).
2. On the server, set `EXTRA_UI_ORIGINS=https://<your-app>.vercel.app`.
3. Vercel → New Project → this repository. `vercel.json` already sets the build. Add the environment variable `VITE_API_BASE=https://antibody.onrender.com`, then deploy.
4. Open the Vercel URL and sign in with an access token.

## Accounts: access tokens

Hosted servers run with `PROJECT_AUTH_MODE=token`. The automatic "local" sign-in is disabled, and everyone signs in with a token tied to one project:

```bash
python -m server.cli invite --project demo_stage --name alice
```

On a container: `docker exec <container> python -m server.cli invite --project demo_stage --name alice`. Send the token privately; it is a password.

## Environment variables

| Variable | Meaning |
|---|---|
| `ANTIBODY_DATA_DIR` | Where all state lives (database, objects, workspaces, secrets). `/data` in the image |
| `PROJECT_AUTH_MODE` | `local` (same-machine sign-in, the default) or `token` (hosted) |
| `API_BIND_HOST`, `API_PORT` (or `PORT`) | Listen address. `0.0.0.0` and `8765` in the image |
| `EXTRA_ALLOWED_HOSTS` | The server's public host names, comma separated |
| `EXTRA_UI_ORIGINS` | Website origins allowed to call the server (for a separately hosted UI) |
| `ANTHROPIC_API_KEY` | Claude for Chat, the safety auditor, lesson diagnosis and API-based merges. It can also be saved in Settings |
| `VITE_API_BASE` | Website build only: the server URL when the UI is hosted separately |

## Check it before deploying

```bash
python scripts/hosted_preview.py
```

This runs a token-mode server on http://127.0.0.1:8790 with its own data folder and a fresh `demo_hosted` project. To create a token for it:

```bash
python scripts/hosted_preview.py --invite me
```

Verified: the sign-in screen accepts the invite, and a fresh project runs the security story (attack blocked, incident with a lesson to evaluate).

## Not yet done (needs your accounts)

- Actually creating the Render/Railway/Fly service and the Vercel project, and connecting the repository (it needs a GitHub remote).
- Building the Docker image. Docker isn't installed on this machine, so the Dockerfile is untested; the same configuration was tested without Docker via `hosted_preview.py`.
- A custom domain and HTTPS certificate. Render and Vercel provide these automatically on their own domains.
