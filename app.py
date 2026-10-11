"""Patch — Streamlit UI for the autonomous triage/patch pipeline.

Run locally from the project root:

    streamlit run app.py

Includes:
  * Manual pipeline runner (repo/file/issue -> diff -> PR)
  * Auto-Track repositories -> background issue polling & auto-triage
  * Live feed of recent automated fixes (manual + auto-generated)
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import streamlit as st
from github import Github, Auth

import config
import core.analyzer
import core.fetcher
import core.pr_creator
import core.tracker

st.set_page_config(page_title="Patch", page_icon=None, layout="centered")

st.title("Patch")
st.caption(
    "Autonomous GitHub issue triage & patch generation."
)

# ---------------------------------------------------------------------------
# Fixes feed (session-lifetime, newest first)
# ---------------------------------------------------------------------------

if "_fixes_feed" not in st.session_state:
    st.session_state._fixes_feed = []


def record_fix(repo, file_path, issue_title, patch, pr_url):
    st.session_state._fixes_feed.insert(
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
    del st.session_state._fixes_feed[50:]


def _tracker_to_feed_callback(repo, file_path, issue_title, patch, pr_url):
    """Called from the background poller thread; must touch session safely."""

    # Streamlit session_state isn't thread-safe cross-thread; feed entries from
    # the background loop go into a module-level list that the UI merges below.
    _bg_fixes.append(
        {
            "repo": repo,
            "file_path": file_path,
            "issue_title": issue_title,
            "patch": patch,
            "pr_url": pr_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


# Module-level feed for background-thread writes (thread-safe via list append
# atomicity); merged into the session feed on each UI rerun.
_bg_fixes: list = []
core.tracker.on_fix_callback = _tracker_to_feed_callback

# ---------------------------------------------------------------------------
# Background poller management
# ---------------------------------------------------------------------------

if "_poller_thread" not in st.session_state:
    st.session_state._poller_thread = None


def _start_poller_background():
    """Run core.tracker.poll_forever in a background thread (once per session)."""
    if st.session_state._poller_thread is not None and st.session_state._poller_thread.is_alive():
        return
    started = threading.Event()

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            started.set()
            loop.run_until_complete(core.tracker.poll_forever(30))
        except Exception as exc:  # noqa: BLE001 - thread must not die silently
            print(f"[streamlit poller] crashed: {exc}")
        finally:
            loop.close()

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    started.wait(timeout=5)
    st.session_state._poller_thread = th


def resolve_ref(repo_name: str, ref: str) -> str:
    """Return the branch to use: the given one, or the repo's default branch."""
    if ref.strip():
        return ref.strip()
    gh = Github(auth=Auth.Token(config.GITHUB_TOKEN))
    return gh.get_repo(repo_name).default_branch


def _gh_client() -> Github:
    """GitHub client using the non-deprecated auth interface."""
    return Github(auth=Auth.Token(config.GITHUB_TOKEN))


# ---------------------------------------------------------------------------
# Sidebar: manual pipeline form
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Manual run")
    repo_name = st.text_input(
        "Repository",
        placeholder="owner/repo",
        help="Any public GitHub repository, e.g. octocat/Hello-World.",
    )

    ref_auto, file_choices = None, None
    if repo_name.strip():
        try:
            gh = _gh_client()
            repo = gh.get_repo(repo_name.strip())
            ref_auto = repo.default_branch
            file_choices = [
                c.path for c in repo.get_contents("", ref=ref_auto) if c.type == "file"
            ]
        except Exception as exc:
            st.warning(f"Could not list repo contents: {exc}")

    ref = st.text_input(
        "Base branch",
        value=ref_auto or "",
        placeholder="auto-detect default branch",
        help="Leave empty to auto-detect the repo's default branch "
        "(or enter one explicitly: main, master, ...).",
    )
    if file_choices:
        file_path = st.selectbox(
            "File path (picked from the repo root)",
            options=file_choices,
        )
    else:
        file_path = st.text_input(
            "File path",
            placeholder="README.md",
            help="Enter the repo-relative path of the file to patch.",
        )
    st.divider()
    st.markdown(
        "**Pipeline**\n"
        "1. `core.fetcher` — clone-free file fetch\n"
        "2. `core.analyzer` — Groq generates a unified diff\n"
        "3. `core.pr_creator` — fork, branch, apply, push, open PR"
    )

    # Auto-Track controls (mirrors the FastAPI dashboard's /api/track section)
    st.divider()
    st.subheader("Auto-Track")
    st.caption("Polls tracked repos every 30s and auto-patches new issues.")

    track_input = st.text_input(
        "Repository to track",
        key="track_repo_input",
        placeholder="owner/repo",
        help="e.g. octocat/Hello-World or your own repo",
    )
    if st.button("▶ Start Tracking", use_container_width=True, disabled=(not track_input.strip())):
        try:
            core.tracker.track(track_input.strip())
            _start_poller_background()
            st.success(f"now tracking {track_input.strip()} — auto-triage armed")
        except Exception as exc:
            st.error(f"failed to track: {exc}")

    st.markdown("**Currently Monitored Repositories**")
    tracked = core.tracker.list_tracked()
    if tracked:
        for entry in tracked:
            col1, col2, col3 = st.columns([1, 3, 1])
            with col1:
                st.markdown("🟢")
            with col2:
                st.markdown(f"**{entry['repo']}**")
                st.caption(f"since {entry['added_at']}")
            with col3:
                if st.button("✕", key=f"untrack::{entry['repo']}"):
                    core.tracker.untrack(entry["repo"])
                    st.rerun()
    else:
        st.caption("No repositories tracked yet.")

# ---------------------------------------------------------------------------
# Main area: manual run form
# ---------------------------------------------------------------------------

st.subheader("Issue")
issue_title = st.text_input("Issue / bug title", placeholder="Crash when name is None")
error_context = st.text_area(
    "Detailed description / error context",
    height=180,
    placeholder=(
        "What is broken, and what should happen instead?\n"
        "e.g. 'README has no languages section — add one listing Python.'"
    ),
    help="This text is the LLM's main signal, so be specific and repro-like.",
)

go = st.button("🚀 Generate Patch & Open PR", type="primary", use_container_width=True)

if go:
    if not repo_name or not file_path or not issue_title or not error_context:
        st.error("All fields are required — repo, file path, title, and description.")
        st.stop()

    st.divider()

    try:
        ref_resolved = resolve_ref(repo_name, ref_auto or ref)
        with st.status(f"Running pipeline… (branch: {ref_resolved})", expanded=False) as status:
            st.write(f"Fetching `{file_path}` from `{repo_name}` (ref `{ref_resolved}`)…")
            file_content = core.fetcher.get_file_content(repo_name, file_path, ref=ref_resolved)

            st.write("Asking Groq to generate a unified diff…")
            patch = core.analyzer.generate_patch(
                file_path, file_content, error_context
            )

            st.write("Forking / branching / applying / opening PR…")
            pull_request_url = core.pr_creator.apply_patch_and_create_pr(
                repo_name, file_path, patch, ref=ref_resolved,
                original_content=file_content,
            )
            status.update(
                label="Pipeline finished ✔", state="complete", expanded=True
            )

        # Feed the manual run into the same fixes feed.
        record_fix(repo_name, file_path, issue_title, patch, pull_request_url)

        st.success("Patch generated and Pull Request opened!")
        st.markdown(f"**[{pull_request_url}]({pull_request_url})**")
        st.code(patch, language="diff")

    except Exception as exc:
        st.error(f"Pipeline failed: {exc}")
        st.caption("Check the server log for the full traceback.")

# ---------------------------------------------------------------------------
# Recent Automated Fixes (manual + auto-generated)
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Recent Automated Fixes")

# Merge background fixes into the session feed (deduped by timestamp+url).
if _bg_fixes:
    seen_keys = {(f["timestamp"], f["pr_url"]) for f in st.session_state._fixes_feed}
    for item in _bg_fixes:
        key = (item["timestamp"], item["pr_url"])
        if key not in seen_keys:
            st.session_state._fixes_feed.append(item)
            seen_keys.add(key)
    _bg_fixes.clear()
    st.session_state._fixes_feed.sort(key=lambda f: f["timestamp"], reverse=True)
    del st.session_state._fixes_feed[50:]

if st.session_state._fixes_feed:
    for f in st.session_state._fixes_feed[:20]:
        with st.container(border=True):
            col_a, col_b = st.columns([4, 1])
            with col_a:
                st.markdown(f"**{f['issue_title']}**")
                st.caption(f"{f['repo']} · {f['file_path']} · {f['timestamp'][:16]}")
            with col_b:
                st.link_button("Open PR", f["pr_url"], use_container_width=True)
            with st.expander("show diff"):
                st.code(f["patch"], language="diff")
else:
    st.caption("No fixes generated yet — they'll appear here automatically.")

# ---------------------------------------------------------------------------
# Auto-poller status (bottom of main area)
# ---------------------------------------------------------------------------

poller_alive = (
    st.session_state._poller_thread is not None
    and st.session_state._poller_thread.is_alive()
)
st.divider()
if poller_alive:
    st.caption("🟢 Auto-triage poller running in the background (every 30s).")
else:
    st.caption("⚪ Auto-triage poller not running — track a repo to arm it.")
