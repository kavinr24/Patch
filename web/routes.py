"""HTTP routes for the Patch dashboard and its APIs.

Session model: each browser gets a `patch_sid` cookie (30-day TTL). The
fixes feed and the tracked-repos lists are scoped per session — one
user's Recent Fixes and Monitored Repositories are never served to
another. The git/Groq pipeline itself is untouched, and the auto-poller
keeps a single shared worklist; fixes it produces are attributed to the
session(s) that track the source repo.
"""

import json
import threading
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
    try:
        entry = core.tracker.track(req.repo)   # shared worklist for the poller
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with _lock:
        lst = _sid_tracked.setdefault(sid, [])
        if not any(e["repo"] == req.repo for e in lst):
            lst.append({"repo": req.repo, "added_at": entry["added_at"]})
            _repo_subscribers.setdefault(req.repo, set()).add(sid)
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
        raise HTTPException(status_code=500, detail=str(exc)) from exc
