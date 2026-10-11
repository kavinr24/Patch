# Patch

Automated bug triage and patching: point Patch at a public GitHub repository
(or a specific file), describe the issue, and it fetches the file, generates
a fix with Groq's LLM, and opens a pull request with the patch — no local
clone required.

## What it does

- **Manual patch generation** — repo + file + issue description in, unified
  diff out, PR opened on GitHub automatically (fork → branch → commit → PR).
- **Auto-track repositories** — register a repo and the bg poller scans its
  latest open issues every 30s, triaging each new issue with the same
  pipeline (cross-run duplicate claiming prevents double PRs).
- **Execution log & diff viewer** — live step-by-step log with syntax-
  highlighted diffs and direct PR links.
- **Per-browser isolation** — each visitor's tracked repos and recent fixes
  are session-scoped; multiple people can use the app without seeing each
  other's data.

## Project layout

```
main.py             FastAPI app assembly + poller wiring
web/                Dashboard template, routes, and static assets
core/fetcher.py     GitHub file fetching (ref auto-detect + path fuzzy-match)
core/analyzer.py    Groq-powered unified diff generation
core/pr_creator.py  Fork/branch/commit/push + open PR
core/tracker.py     Repo registry, issue polling, and duplicate-claim engine
config.py           Env-var loading with explicit startup errors
```

## Running locally

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in GITHUB_TOKEN and GROQ_API_KEY
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/ — the dashboard is at `/`, and `/health` returns
`{"status": "ok"}` when up.

Required `.env` values:

| Variable       | Used for                                    |
|----------------|---------------------------------------------|
| `GITHUB_TOKEN` | File fetch, forks, PR creation, commenting   |
| `GROQ_API_KEY` | LLM patch generation (Groq, free tier ok)    |

## Deploying to Render

1. Push this repo to GitHub (it already deploys from `main`).
2. In the Render dashboard: **New → Blueprint**, pick the repo — the bundled
   `render.yaml` pre-fills everything.
3. Add the two env vars (`GROQ_API_KEY`, `GITHUB_TOKEN`) when prompted, then
   apply. Render builds with `pip install -r requirements.txt` and starts
   with `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Health check: `/health` (pre-configured in the Blueprint).

**Deploy notes**

- Env vars are fail-fast: if either secret is missing the boot log tells you
  exactly which one and where to set it.
- Free/Starter instances have an **ephemeral disk** — tracked-repo
  registries and session state reset on every deploy/restart. Use a paid
  instance with a persistent disk if you need tracking to survive.
- Instances idle-spin on the free tier: the first request after ~15 idle
  minutes pays a ~30s cold start.
- Keep tokens scoped: the GitHub PAT needs `repo` (forks/PRs/comments) but
  nothing sensitive beyond that.
