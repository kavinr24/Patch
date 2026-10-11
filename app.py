"""Patch — Streamlit UI for the autonomous triage/patch pipeline.

Run locally from the project root:

    streamlit run app.py

Then open the browser URL it prints (default http://localhost:8501),
fill in the target repo/file and the issue details, and click
"Generate Patch & Open PR" to run the full pipeline end to end:
fetch -> Groq diff -> sanitize/repair -> fork -> commit -> PR URL.
"""

from dotenv import load_dotenv

load_dotenv()

import streamlit as st
from github import Github

import config
import core.analyzer
import core.fetcher
import core.pr_creator

st.set_page_config(page_title="Patch", page_icon="🩹", layout="centered")

st.title("🩹 Patch")
st.caption(
    "Autonomous GitHub issue triage & patch generation — fetch, LLM-diff, "
    "and open a PR on any public repo."
)

def resolve_ref(repo_name: str, ref: str) -> str:
    """Return the branch to use: the given one, or the repo's default branch."""
    if ref.strip():
        return ref.strip()
    gh = Github(config.GITHUB_TOKEN)
    return gh.get_repo(repo_name).default_branch


with st.sidebar:
    st.header("Target")
    repo_name = st.text_input(
        "Repository",
        placeholder="owner/repo",
        help="Any public GitHub repository, e.g. octocat/Hello-World.",
    )

    ref_auto, file_choices = None, None
    if repo_name.strip():
        try:
            gh = Github(config.GITHUB_TOKEN)
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
        ref = resolve_ref(repo_name, ref_auto or ref)
        with st.status(f"Running pipeline… (branch: {ref})", expanded=False) as status:
            st.write(f"Fetching `{file_path}` from `{repo_name}` (ref `{ref}`)…")
            file_content = core.fetcher.get_file_content(repo_name, file_path, ref=ref)

            st.write("Asking Groq to generate a unified diff…")
            patch = core.analyzer.generate_patch(
                file_path, file_content, error_context
            )

            st.write("Forking / branching / applying / opening PR…")
            pull_request_url = core.pr_creator.apply_patch_and_create_pr(
                repo_name, file_path, patch, ref=ref
            )
            status.update(
                label="Pipeline finished ✔", state="complete", expanded=True
            )

        st.success("Patch generated and Pull Request opened!")
        st.markdown(f"**[{pull_request_url}]({pull_request_url})**")
        st.code(patch, language="diff")

    except Exception as exc:
        st.error(f"Pipeline failed: {exc}")
        st.caption("Check the server log for the full traceback.")
