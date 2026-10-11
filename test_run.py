"""Local test runner for the Patch pipeline.

Runs the core modules directly -- no FastAPI server or GitHub webhook needed.

How to run (from the project root):

    python test_run.py

Optionally pass a repo, file path and ref to test a specific target (defaults
to a small public repo so no write access is needed):

    python test_run.py github/gitignore Python.gitignore main

Requires a valid GROQ_API_KEY and GITHUB_TOKEN in .env (see .env.example).
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import core.analyzer
import core.fetcher


def run_pipeline_test(repo_name: str, file_path: str, ref: str) -> None:
    """Fetch a file via core.fetcher, then patch it via core.analyzer."""
    print(f"[1/2] Fetching '{file_path}' from '{repo_name}' (ref={ref}) ...")
    content = core.fetcher.get_file_content(repo_name, file_path, ref=ref)
    print(f"      fetched {len(content)} chars")

    print("[2/2] Asking Groq to generate a patch ...")
    patch = core.analyzer.generate_patch(
        file_path, content, f"Local test run on {repo_name}/{file_path}"
    )
    print(f"      got patch of {len(patch)} chars")
    print("--- patch preview (first 300 chars) ---")
    print(patch[:300])
    print("--- end preview ---")
    print("PIPELINE OK")


def run_module_self_tests() -> None:
    """Cheap wiring checks that don't hit the Groq API."""
    assert callable(core.fetcher.get_file_content), "core.fetcher not wired"
    assert callable(core.analyzer.generate_patch), "core.analyzer not wired"
    assert core.analyzer.generate_patch.__name__ == "generate_patch"
    print("module self-tests OK (no network)")


if __name__ == "__main__":
    if len(sys.argv) == 4:
        repo, path, ref = sys.argv[1], sys.argv[2], sys.argv[3]
    elif len(sys.argv) == 1:
        # Small, harmless default: public repo, no write access required.
        repo, path, ref = "octocat/Hello-World", "README", "master"
    else:
        print(__doc__)
        sys.exit(2)

    run_module_self_tests()
    run_pipeline_test(repo, path, ref)
