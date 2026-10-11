"""HTTP routes for the Patch dashboard and its APIs.

Session model: each browser gets a `patch_sid` cookie (30-day TTL). The
fixes feed and the tracked-repos lists are scoped per session — one
user's Recent Fixes and Monitored Repositories are never served to
another. The git/Groq pipeline itself is untouched, and the auto-poller
keeps a single shared worklist; fixes it produces are attributed to the
session(s) that track the source repo.

Cost protection: an in-memory sliding-window limiter gates the expensive
endpoints (patch pipeline, tracking) per session AND globally so a
runaway client (or several) can't burn through the Groq/GitHub quotas.
Upstream error text (GitHub/Groq subtleties, internal URLs) is mapped to
safe, human messages before it reaches the client.
"""

import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import core.tracker
from web.assets import ICON_PATH, load_dashboard_html

router = APIRouter()

# ------------------------------------------------------------------------ --
# Per-session state
# ------------------------------------------------------------------------ --

SESSION_COOKIE = "patch_sid"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_FIXES_BUDGET = 50                   # max entries kept per session

_SESSIONS_PATH = Path(__file__).resolve().parent / "_tracked_sessions.json"

_lock = threading.Lock()             # guards the maps below (poller thread + handlers)
_sid_tracked: dict[str, list[dict]] = {}          # sid -> [{repo, added_at}, ...]
_repo_subscribers: dict[str, set[str]] = {}       # repo -> {sid, ...}
_fixes_by_sid: dict[str, list[dict]] = {}         # sid -> [entry, ...] newest first

# ------------------------------------------------------------------------ --
# Rate limiting (in-memory sliding window; per-process, resets on restart
# alongside everything else). Two budgets: per-session, and a global cap
# so several concurrent users can't collectively exhaust the quotas.
# ------------------------------------------------------------------------ --

_RL_LOCK = threading.Lock()
_MOVE_TIMES: dict[str, list[float]] = {}          # bucket -> [timestamps]

# (window_seconds, max_events) per bucket
_LIMITS = {
    "global_pipeline": (60, 6),     # <=6 patch runs/min across ALL users
    "per_sid_pipeline": (60, 3),    # each user: <=3 runs/min; 1 run ~5-20s
    "global_track": (60, 10),
    "per_sid_track": (60, 4),
}


def _check_rate(bucket: str) -> None:
    """Raise 429 HTTPException when the bucket's window budget is spent."""
    window, max_events = _LIMITS[bucket]
    now = time.monotonic()
    with _RL_LOCK:
        times = [t for t in _MOVE_TIMES.setdefault(bucket, []) if now - t < window]
        if len(times) >= max_events:
            retry = int(window - (now - times[0])) + 1
            _MOVE_TIMES[bucket] = times
            raise HTTPException(
                status_code=429,
                detail=f"Too many requests — try again in {retry}s.",
            )
        times.append(now)
        _MOVE_TIMES[bucket] = times
        # opportunistic sweep of other buckets (cheap, under the same lock)
        for b, ts in list(_MOVE_TIMES.items()):
            w = _LIMITS[b][0]
            _MOVE_TIMES[b] = [t for t in ts if now - t < w]


# ------------------------------------------------------------------------ --
# Upstream error sanitization: never leak GitHub/Groq internals to clients.
# ------------------------------------------------------------------------ --

def _safe_detail(exc: Exception) -> str:
    """Map known pipeline errors to human messages; hide everything else."""
    raw = f"{type(exc).__name__}: {exc}"
    low = raw.lower()
    if "no commit found for the ref" in low or "not found" in low and "404" in low:
        return ("That repository or file doesn't exist (or isn't public). "
                "Double-check the owner/repo and file path.")
    if "404" in low:
        return ("The repository or file couldn't be found. Check the repo "
                "name and file path — the extension is optional.")
    if "rate limit" in low or "429" in low or "secondary rate" in low:
        return ("Upstream rate limit hit (GitHub or Groq). Wait a minute "
                "and try again.")
    if "bad credentials" in low or "401" in low or "unauthorized" in low:
        return ("The server's API token was rejected. Ping the maintainer — "
                "this isn't something you did.")
    if "groq" in low or "too large" in low or "context length" in low:
        return ("The LLM couldn't process this file (too big or awkward to "
                "patch). Try a smaller or more targeted file.")
    if "nothing to commit" in low:
        return ("The generated patch made no changes to the file. Try a more "
                "detailed issue description.")
    if "trustanchors" in low or "certificate" in low or "ssl" in low:
        return "TLS/network hiccup reaching GitHub or Groq. Try again shortly."
    # Unknown failure: log liberally server-side, say nothing useful client-side
    print(f"[routes] unmapped pipeline error: {raw}")
    return "Pipeline failed unexpectedly. The server log has the details."


class IssuePayload(BaseModel):
    repo_name: str
    file_path: str
    issue_title: str
    error_context: str
    ref: str = ""  # empty/omitted -> auto-detect the repo's default branch


class TrackRequest(BaseModel):
    repo: str


# ------------------------------------------------------------------ state IO

def _load_sessions() -> None:
    if _SESSIONS_PATH.exists():
        try:
            data = json.loads(_SESSIONS_PATH.read_text(encoding="utf-8"))
            for sid, entries in data.items():
                _sid_tracked[sid] = list(entries)
                for e in entries:
                    _repo_subscribers.setdefault(e["repo"], set()).add(sid)
        except (OSError, ValueError, KeyError) as exc:
            print(f"[routes] failed to load {_SESSIONS_PATH}: {exc}")


def _save_sessions() -> None:
    try:
        _SESSIONS_PATH.write_text(
            json.dumps(_sid_tracked, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[routes] failed to save {_SESSIONS_PATH}: {exc}")


def startup_sync() -> None:
    """Run once at app boot (before the poller starts).

    Potentially removes legacy globally-registered repos that no session
    subscribes to, and re-registers anything found in the sessions file.
    """
    _load_sessions()
    for sid, entries in _sid_tracked.items():
        for e in entries:
            core.tracker.track(e["repo"])
    for repo in [r["repo"] for r in core.tracker.list_tracked()]:
        if not _repo_subscribers.get(repo):
            core.tracker.untrack(repo)  # no session left behind after restarts


# ------------------------------------------------------------ session plumbing

def _require_sid(request: Request) -> tuple[str, Response | None]:
    """Return (sid, set_cookie_response).

    The second element is a fresh Response carrying the Set-Cookie header when
    the caller must issue one — the endpoint MUST return it (or copy its
    headers) instead of building its own, or the cookie is silently dropped.
    """
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        sid = uuid.uuid4().hex
        resp = Response(status_code=200)
        resp.set_cookie(SESSION_COOKIE, sid, max_age=_COOKIE_MAX_AGE)
        return sid, resp
    return sid, None


# --------------------------------------------------------------- fixes feed

def _push_fix(sid: str, entry: dict) -> None:
    lst = _fixes_by_sid.setdefault(sid, [])
    lst.insert(0, entry)
    del lst[_FIXES_BUDGET:]


def record_fix(
    repo: str, file_path: str, issue_title: str, patch: str, pr_url: str,
    sids: list[str] | None = None,
) -> None:
    """Records a completed fix into each owning session's feed.

    Called directly by manual pipeline runs (with the caller's sid), and as
    the core.tracker poller callback (no sids) — in which case the fix goes
    to every session subscribed to that repo.
    """
    if sids is None:
        with _lock:
            sids = list(_repo_subscribers.get(repo.strip(), ()))
    entry = {
        "repo": repo,
        "file_path": file_path,
        "issue_title": issue_title,
        "patch": patch,
        "pr_url": pr_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        for sid in sids or ():
            _push_fix(sid, entry)


# ------------------------------------------------------------------ pages --

@router.get("/")
def dashboard(request: Request) -> HTMLResponse:
    sid, cookie_resp = _require_sid(request)
    html = HTMLResponse(content=load_dashboard_html(), status_code=200)
    if cookie_resp is not None:
        html.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    return html


@router.get("/icon.png", include_in_schema=False)
def icon() -> FileResponse:
    if not ICON_PATH.is_file():
        raise HTTPException(status_code=404, detail="icon.png not found in project root")
    return FileResponse(ICON_PATH, media_type="image/png")


# ------------------------------------------------------------------- APIs --

@router.get("/api/fixes")
def api_fixes(request: Request, response: Response):
    sid, cookie_resp = _require_sid(request)
    if cookie_resp is not None:
        response.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    with _lock:
        return {"fixes": list(_fixes_by_sid.get(sid, []))}


@router.post("/api/track")
def api_track(req: TrackRequest, request: Request, response: Response):
    sid, cookie_resp = _require_sid(request)
    if cookie_resp is not None:
        response.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    _check_rate("global_track")
    _check_rate("per_sid_track")
    repo = req.repo.strip()
    # Basic shape validation: owner/repo only, no paths or URLs.
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        raise HTTPException(
            status_code=422,
            detail="Repository must look like 'owner/repo' — no paths or URLs.",
        )
    try:
        entry = core.tracker.track(repo)   # shared worklist for the poller
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=_safe_detail(exc)) from exc

    with _lock:
        lst = _sid_tracked.setdefault(sid, [])
        if not any(e["repo"] == repo for e in lst):
            lst.append({"repo": repo, "added_at": entry["added_at"]})
            _repo_subscribers.setdefault(repo, set()).add(sid)
            _save_sessions()
    return {"status": "tracked", "entry": entry}


@router.delete("/api/track/{owner}/{repo}")
def api_untrack(owner: str, repo: str, request: Request, response: Response):
    sid, cookie_resp = _require_sid(request)
    if cookie_resp is not None:
        response.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    full = f"{owner}/{repo}"
    with _lock:
        lst = _sid_tracked.get(sid, [])
        if not any(e["repo"] == full for e in lst):
            raise HTTPException(status_code=404, detail=f"{full} is not tracked")
        _sid_tracked[sid] = [e for e in lst if e["repo"] != full]
        _save_sessions()

        subs = _repo_subscribers.get(full, set())
        subs.discard(sid)
        if not subs:
            # Last session out: stop the shared poller for this repo.
            _repo_subscribers.pop(full, None)
            core.tracker.untrack(full)
    return {"status": "untracked", "repo": full}


@router.get("/api/tracked")
def api_tracked(request: Request, response: Response):
    sid, cookie_resp = _require_sid(request)
    if cookie_resp is not None:
        response.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    with _lock:
        return {"tracked": list(_sid_tracked.get(sid, []))}


@router.get("/health")
def health_check():
    return {"status": "ok"}


@router.post("/webhook/trigger-fix")
def trigger_fix(payload: IssuePayload, request: Request, response: Response):
    # Imported here so the dashboard/API layer stays importable without the
    # Groq/PyGithub stack resolving at module import time.
    import core.analyzer
    import core.fetcher
    import core.pr_creator

    sid, cookie_resp = _require_sid(request)
    if cookie_resp is not None:
        response.headers["set-cookie"] = cookie_resp.headers["set-cookie"]
    _check_rate("global_pipeline")
    _check_rate("per_sid_pipeline")
    try:
        # Empty ref -> auto-detect the repo's default branch (some repos use
        # 'master', 'trunk' etc.; a hardcoded 'main' 404s on those).
        ref = payload.ref.strip() or core.fetcher.get_default_branch(payload.repo_name)
        file_content = core.fetcher.get_file_content(
            payload.repo_name, payload.file_path, ref=ref
        )
        patch = core.analyzer.generate_patch(
            payload.file_path, file_content, payload.error_context
        )
        pull_request_url = core.pr_creator.apply_patch_and_create_pr(
            payload.repo_name, payload.file_path, patch, ref=ref,
            original_content=file_content,
        )
        record_fix(
            payload.repo_name, payload.file_path, payload.issue_title,
            patch, pull_request_url, sids=[sid],
        )
        return {"status": "success", "pull_request_url": pull_request_url, "patch": patch}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - single failure point for the webhook
        raise HTTPException(status_code=502, detail=_safe_detail(exc)) from exc
