"""Tracked-repository registry, background polling, and auto-triage engine.

Persists tracked repositories to ``tracked_repos.json`` (in-memory dict with
file fallback). Format per entry::

    {
        "repo": "owner/repo",
        "added_at": "2026-10-10T20:30:00.123456+00:00",
        "processed_issue_ids": [123, 456]
    }

``poll_forever`` runs every 30 seconds. For each tracked repo it lists open
issues, skips already-processed ones and bot-authored ones, picks a target
file, runs the full patch pipeline (fetch -> Groq diff -> fork/PR), posts a
comment on the issue with the PR link, and records the issue id so it never
triggers again.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

PERSIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tracked_repos.json")
DEFAULT_POLL_SECONDS = 30

# Popular default targets when the issue text gives no file hints.
DEFAULT_FILE_CANDIDATES = ("main.py", "app.py", "README.md", "README")

# Issues authored by these logins (or flagged bot) are skipped.
BOT_LOGIN_MARKERS = ("patch", "bot[bot]", "github-actions")

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
    """All entries, sorted by addition time."""
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


# --------------------------------------------------------------------------
# GitHub client and issue scanning
# --------------------------------------------------------------------------

def _gh():
    from github import Github, Auth

    import config

    return Github(auth=Auth.Token(config.GITHUB_TOKEN))


def _is_bot_authored(issue) -> bool:
    login = getattr(issue.user, "login", "") or ""
    lowered = login.lower()
    if lowered.endswith("[bot]") or any(m in lowered for m in BOT_LOGIN_MARKERS):
        return True
    if getattr(issue.user, "type", "") == "Bot":
        return True
    return False


def _has_bot_pr_comment(issue) -> bool:
    """True if any comment on the issue points at a patch/fix-* PR head."""
    try:
        for c in issue.get_comments():
            if "patch/fix-" in (c.body or ""):
                return True
    except Exception as exc:  # noqa: BLE001 - comment check must not kill polling
        print(f"[tracker]   comment scan failed on issue: {exc}")
    return False


def _list_candidate_issues(repo_full_name: str, max_issues: int = 10) -> list:
    """Open issues on a tracked repo that are new/unhandled, newest first."""
    from github import Github  # noqa: F401  (client built via _gh())

    gh = _gh()
    repo_obj = gh.get_repo(repo_full_name)
    entry = _registry.get(repo_full_name) or {}
    processed = set(entry.get("processed_issue_ids", []))

    candidates = []
    examined = 0
    MAX_EXAMINE = 100  # bound API page pulls: issues come newest-first
    # PaginatedList is lazy; stop when enough new candidates are gathered.
    for issue in repo_obj.get_issues(state="open"):
        if len(candidates) >= max_issues or examined >= MAX_EXAMINE:
            break
        examined += 1
        if issue.number in processed:
            continue
        if _is_bot_authored(issue):
            continue
        if issue.pull_request:
            continue
        if _has_bot_pr_comment(issue):
            continue
        candidates.append(issue)
    return candidates


def extract_file_paths(text: str) -> list[str]:
    """Pull plausible file paths out of issue text/tracebacks.

    Looks for extensions-and-slashes patterns as well as bare filenames with
    known code extensions. Returns unique paths, best guess first.
    """
    if not text:
        return []
    patterns = [
        r"\b[\w\-./\\]+\.(?:py|js|ts|tsx|jsx|md|json|yml|yaml|toml|txt|html|css|sh|go|rs|java|rb|php|c|cpp|h)\b",
    ]
    found: list[str] = []
    for pat in patterns:
        for m in re.findall(pat, text):
            candidate = m.replace("\\", "/").lstrip("./")
            if not candidate or "/" in candidate and candidate.startswith("http"):
                continue
            if candidate not in found:
                found.append(candidate)
    return found


def pick_target_file(issue, repo_root_files: list[str]) -> str:
    """Choose the file the issue is about.

    1st: file mentioned in title/body/traceback that exists in the repo root.
    2nd: default candidates (main.py, app.py, README.md) present in the repo.
    Falls back to the first root file found, raise if the repo has none.
    """
    import core.fetcher as _f  # noqa: F401  (kept for parity; not strictly needed)

    text = f"{issue.title or ''}\n{issue.body or ''}"
    mentioned = extract_file_paths(text)

    from github import Github  # noqa: F401

    gh = _gh()
    repo_obj = gh.get_repo(issue.repository.full_name)
    root_files = repo_root_files or [
        c.path for c in repo_obj.get_contents("", ref=repo_obj.default_branch)
        if getattr(c, "type", "") == "file"
    ]
    lowered_root = {f.lower(): f for f in root_files}
    for path in mentioned:
        if path in root_files:
            return path
        base = path.split("/")[-1]
        if base in lowered_root:
            return lowered_root[base]
    for cand in DEFAULT_FILE_CANDIDATES:
        if cand in root_files:
            return cand
    if root_files:
        return root_files[0]
    raise RuntimeError(f"No file candidates found in {issue.repository.full_name}")


def post_issue_comment(issue, pr_url: str) -> None:
    body = (
        "🤖 **Patch Bot**: I've generated an automated fix for this issue! "
        f"View the PR here: {pr_url}"
    )
    issue.create_comment(body)


def process_issue(issue, ref: str) -> str:
    """Run the full patch pipeline for a single issue; returns the PR URL."""
    import core.analyzer
    import core.fetcher
    import core.pr_creator

    repo_name = issue.repository.full_name
    print(f"[tracker]   processing #{issue.number}: {issue.title!r} on {repo_name}")

    file_path = pick_target_file(issue, repo_root_files=[])
    print(f"[tracker]     target file: {file_path}")

    file_content = core.fetcher.get_file_content(repo_name, file_path, ref=ref)
    error_context = (
        f"{issue.title}\n\n{issue.body or '(no body provided)'}"
    )
    patch = core.analyzer.generate_patch(file_path, file_content, error_context)
    pr_url = core.pr_creator.apply_patch_and_create_pr(
        repo_name, file_path, patch, ref=ref, original_content=file_content,
    )
    print(f"[tracker]     PR: {pr_url}")
    return pr_url


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------

on_fix_callback = None  # optional hook (repo, file_path, issue_title, patch, pr_url)


async def poll_once() -> None:
    """Single polling pass over every tracked repo, triaging new issues."""
    loop = asyncio.get_running_loop()
    for repo_name in list(_registry.keys()):
        entry = _registry.get(repo_name)
        if not entry:
            continue  # may have been removed mid-iteration
        try:
            repo_obj = (await loop.run_in_executor(None, _gh)).get_repo(repo_name)
            ref = repo_obj.default_branch
            issues = await loop.run_in_executor(None, _list_candidate_issues, repo_name)
            print(
                f"[tracker] {repo_name}: {len(issues)} new unhandled issue(s)"
            )
            for issue in issues:
                try:
                    pr_url = await loop.run_in_executor(None, process_issue, issue, ref)
                    await loop.run_in_executor(None, post_issue_comment, issue, pr_url)
                    mark_processed(repo_name, issue.number)
                    if on_fix_callback:
                        # Pull the generated patch text back for the feed.
                        patch_text = ""
                        try:
                            pr = _gh().get_repo(repo_name).get_pull(
                                int(pr_url.rstrip('/').split('/')[-1])
                            )
                            patch_text = f"{pr.title}\n{pr.html_url}"
                        except Exception:
                            pass
                        on_fix_callback(
                            repo_name,
                            getattr(issue, 'file_path_hint', '(auto-picked)'),
                            issue.title,
                            patch_text,
                            pr_url,
                        )
                    print(f"[tracker]   #{issue.number}: done, commented, recorded")
                except Exception as exc:  # noqa: BLE001 - one issue must not stop the rest
                    print(
                        f"[tracker]   #{issue.number}: pipeline failed: {exc}"
                    )
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
