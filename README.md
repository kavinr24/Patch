# Patch

<img width="400" height="400" alt="free-crave-81028562 figma site_" src="https://github.com/user-attachments/assets/d39e3573-7691-41ba-84aa-3f50eabe700a" />

Patch is a bot that reads the issue, fixes the code, and opens the PR for you — just watch it do all the work.

## What it does

Paste in a repo, a file, and a description of the bug. Patch fetches the file straight from GitHub, asks Groq's LLM to write the fix as a diff, applies it, commits it to a fork, and opens a real pull request. No cloning anything, no leaving the dashboard.

There's also an auto-track mode: register a repo and a background poller keeps an eye on its open issues. Anything new gets run through the same pipeline automatically, so issues you post get a PR within a couple of minutes without you touching anything. It keeps track of what it's already fixed too, so you don't end up with ten duplicate PRs on the same issue.

The dashboard shows a live log of every step and the actual diff it's about to ship.

## Tech stack

- **Python** (FastAPI + uvicorn) — the whole backend
- **Groq API** — the LLM that generates the patches
- **GitHub API** (PyGithub) — fetching files, forking, pushing branches, opening PRs
- **Plain HTML/JS + Tailwind + Alpine.js** — the dashboard, nothing fancier
- Deployed on **Render**

## Running it locally

Needs Python 3.11+ and two API keys.

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then put `GITHUB_TOKEN` and `GROQ_API_KEY` in that `.env` file (a classic GitHub PAT with `repo` scope, and a Groq key from console.groq.com). Start it:

```
uvicorn main:app --reload
```

Go to http://127.0.0.1:8000 — that's the whole app. If either key is missing, the boot log tells you exactly which one.

## Dev tools

Built with VSCode, GitHub Copilot, Opencode.
