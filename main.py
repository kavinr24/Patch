from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import core.analyzer
import core.fetcher
import core.pr_creator

app = FastAPI(title="Patch")


class IssuePayload(BaseModel):
    repo_name: str
    file_path: str
    issue_title: str
    error_context: str
    ref: str = "main"


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/webhook/trigger-fix")
def trigger_fix(payload: IssuePayload):
    try:
        file_content = core.fetcher.get_file_content(
            payload.repo_name, payload.file_path, ref=payload.ref
        )
        patch = core.analyzer.generate_patch(
            payload.file_path, file_content, payload.error_context
        )
        pull_request_url = core.pr_creator.apply_patch_and_create_pr(
            payload.repo_name, payload.file_path, patch, ref=payload.ref,
            original_content=file_content,
        )
        return {"status": "success", "pull_request_url": pull_request_url}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - single failure point for the webhook
        raise HTTPException(status_code=500, detail=str(exc)) from exc
