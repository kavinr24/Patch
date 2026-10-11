"""Tracked-repository registry and background polling engine.

Persists tracked repositories to ``tracked_repos.json`` (in-memory dict with
file fallback). Format per entry::

    {
        "repo": "owner/repo",
        "added_at": "2026-10-10T20:30:00.123456+00:00",
        "processed_issue_ids": [123, 456]
    }

The poller (``poll_forever``) runs every 30 seconds, lists open issues
for each tracked repo, and writes its activity to stdout. Patch generation is
deliberately NOT invoked here yet — wiring comes later.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional

# Lazy import - github only needed when actually querying issues
# (kept out of module scope so importing tracker is cheap for tests too).

PERSIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tracked_repos.json")
DEFAULT_POLL_SECONDS = 30

# repo_name (str) -> {repo, added_at, processed_issue_ids}
_registry: dict[str, dict] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry() -> dict[str, dict]:
    """Load persisted entries from disk into memory (best-effort)."""
    global _registry
    if os.path.exists(PERSIST_PATH):
        try:
            with open(PERSIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _registry = {
                entry["repo"]: entry
                for entry in data
                if isinstance(entry, dict) and "repo" in entry
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"[tracker] failed to load {PERSIST_PATH}: {exc}")
            _registry = {}
    return _registry


def save_registry() -> None:
    """Persist the current registry to disk (best-effort; failure is logged)."""
    try:
        with open(PERSIST_PATH, "w", encoding="utf-8") as f:
            json.dump(list(_registry.values()), f, indent=2)
    except OSError as exc:
        print(f"[tracker] failed to save {PERSIST_PATH}: {exc}")


def track(repo: str) -> dict:
    """Add a repo to the registry (idempotent). Returns the entry."""
    repo = repo.strip()
    if not repo:
        raise ValueError("repo must be non-empty")
    entry = _registry.get(repo)
    if entry:
        return entry
    entry = {"repo": repo, "added_at": now_iso(), "processed_issue_ids": []}
    _registry[repo] = entry
    save_registry()
    return entry


def untrack(repo: str) -> bool:
    """Remove a repo from the registry. Returns True if it was present."""
    repo = repo.strip()
    if repo in _registry:
        del _registry[repo]
        save_registry()
        return True
    return False


def get_entry(repo: str) -> Optional[dict]:
    return _registry.get(repo.strip())


def list_tracked() -> list[dict]:
    """All entries, sorted by addition time, plus a live issue count snapshot."""
    return [
        {
            "repo": e["repo"],
            "added_at": e["added_at"],
            "processed_issue_ids": e.get("processed_issue_ids", []),
        }
        for e in sorted(_registry.values(), key=lambda e: e["added_at"])
    ]


def mark_processed(repo: str, issue_id: int) -> None:
    entry = _registry.get(repo.strip())
    if entry and issue_id not in entry["processed_issue_ids"]:
        entry["processed_issue_ids"].append(issue_id)
        save_registry()


def _list_open_issues(repo_full_name: str, max_pages: int = 1) -> list[tuple[int, str]]:
    """Fetch open issue (id, title) pairs for a repo, via the stored token.

    Limits to ``max_pages`` pages of 100 to keep poll cycles bounded.
    """
    from github import Github, Auth
    import config

    gh = Github(auth=Auth.Token(config.GITHUB_TOKEN))
    repo = gh.get_repo(repo_full_name)
    out = []
    # PaginatedList iterates lazily page-by-page; cap total issues scanned.
    limit = max_pages * 100
    for issue in repo.get_issues(state="open"):
        if len(out) >= limit:
            break
        if issue.pull_request:
            continue
        out.append((issue.number, issue.title))

    return out


async def poll_once() -> None:
    """Single polling pass over every tracked repo."""
    loop = asyncio.get_running_loop()
    for repo_name in list(_registry.keys()):
        entry = _registry.get(repo_name)
        if not entry:
            continue  # may have been removed mid-iteration
        try:
            issues = await loop.run_in_executor(None, _list_open_issues, repo_name)
            processed = set(entry.get("processed_issue_ids", []))
            new = [(num, title) for num, title in issues if num not in processed]
            print(
                f"[tracker] {repo_name}: {len(issues)} open issue(s), "
                f"{len(new)} unprocessed"
            )
            # Patch generation intentionally not called yet (see module docstring).
            for num, title in new:
                print(f"[tracker]   would process #{num}: {title}")
        except Exception as exc:  # noqa: BLE001 - poll must survive repo errors
            print(f"[tracker] {repo_name}: poll failed: {exc}")


async def poll_forever(interval_seconds: int = DEFAULT_POLL_SECONDS) -> None:
    """Background loop; runs forever until cancelled."""
    load_registry()
    while True:
        try:
            await poll_once()
        except Exception as exc:  # noqa: BLE001 - loop must never die
            print(f"[tracker] poll_once crashed: {exc}")
        await asyncio.sleep(interval_seconds)
