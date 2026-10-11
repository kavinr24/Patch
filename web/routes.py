"""HTTP routes for the Patch dashboard and its APIs."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import core.tracker
from web.assets import ICON_PATH, load_dashboard_html

router = APIRouter()


class IssuePayload(BaseModel):
    repo_name: str
    file_path: str
    issue_title: str
    error_context: str
    ref: str = ""  # empty/omitted -> auto-detect the repo's default branch


class TrackRequest(BaseModel):
    repo: str


# In-memory feed of automated fixes (newest first). Process-lifetime only.
_fixes_feed: list[dict] = []


def record_fix(
    repo: str, file_path: str, issue_title: str, patch: str, pr_url: str,
) -> None:
    """Callback wired into core.tracker by the app lifespan."""
    _fixes_feed.insert(
        0,
        {
            "repo": repo,
            "file_path": file_path,
            "issue_title": issue_title,
            "patch": patch,
            "pr_url": pr_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    del _fixes_feed[50:]


# ------------------------------------------------------------------ pages --

@router.get("/")
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=load_dashboard_html(), status_code=200)


@router.get("/icon.png", include_in_schema=False)
def icon() -> FileResponse:
    if not ICON_PATH.is_file():
        raise HTTPException(status_code=404, detail="icon.png not found in project root")
    return FileResponse(ICON_PATH, media_type="image/png")


# ------------------------------------------------------------------- APIs --

@router.get("/api/fixes")
def api_fixes():
    return {"fixes": _fixes_feed}


@router.post("/api/track")
def api_track(req: TrackRequest):
    try:
        entry = core.tracker.track(req.repo)
        return {"status": "tracked", "entry": entry}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/api/track/{owner}/{repo}")
def api_untrack(owner: str, repo: str):
    removed = core.tracker.untrack(f"{owner}/{repo}")
    if not removed:
        raise HTTPException(status_code=404, detail=f"{owner}/{repo} is not tracked")
    return {"status": "untracked", "repo": f"{owner}/{repo}"}


@router.get("/api/tracked")
def api_tracked():
    return {"tracked": core.tracker.list_tracked()}


@router.get("/health")
def health_check():
    return {"status": "ok"}


@router.post("/webhook/trigger-fix")
def trigger_fix(payload: IssuePayload):
    # Imported here so the dashboard/API layer stays importable without the
    # Groq/PyGithub stack resolving at module import time.
    import core.analyzer
    import core.fetcher
    import core.pr_creator

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
            patch, pull_request_url,
        )
        return {"status": "success", "pull_request_url": pull_request_url, "patch": patch}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - single failure point for the webhook
        raise HTTPException(status_code=500, detail=str(exc)) from exc
