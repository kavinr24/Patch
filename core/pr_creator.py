"""Pull request creation from applied patches.

Implements the Fork-and-PR workflow so the pipeline can submit pull
requests to any public repository, even when the authenticated token
has no direct write access to the target:

    1. Ensure the authenticated user has a fork of the upstream repo.
    2. Clone the *fork* (not upstream) into a temporary directory.
    3. Create a branch, apply the diff, commit, and push to the fork.
    4. Open a PR from fork-branch -> upstream base branch via PyGithub.
"""

import os
import re
import subprocess
import tempfile
import time

from github import Github

import config


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, raising RuntimeError with captured output on failure."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _ensure_fork(gh: Github, upstream) -> object:
    """Return the authenticated user's fork of `upstream`, creating it if needed."""
    login = gh.get_user().login
    fork_full_name = f"{login}/{upstream.name}"
    try:
        return gh.get_repo(fork_full_name)
    except Exception:
        # Fork creation is asynchronous on GitHub's side; poll briefly.
        try:
            fork = upstream.create_fork()
            for attempt in range(10):
                try:
                    return gh.get_repo(fork_full_name)
                except Exception:
                    if attempt == 9:
                        raise
                    time.sleep(1.5)
            return fork
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create or access fork {fork_full_name}: {exc}"
            ) from exc


def _ensure_diff_header(diff_text: str, file_path: str) -> str:
    """Repair diffs that came back with bare ---/+++ header lines (no paths).

    LLMs sometimes emit unchanged-context headers like just '---' and '+++'.
    We know the target file, so fill the paths in; leave proper headers alone.
    """
    lines = diff_text.splitlines()
    fixed = []
    for line in lines:
        if line.strip() == "---":
            fixed.append(f"--- a/{file_path}")
        elif line.strip() == "+++":
            fixed.append(f"+++ b/{file_path}")
        else:
            fixed.append(line)
    return "\n".join(fixed)


def _fix_hunk_counts(diff_text: str) -> str:
    """Recompute hunk headers (`@@ -a,b +c,d @@`) with correct line counts.

    LLMs frequently miscount context/addition lines, producing corrupt
    patches. Counting the actual body lines per hunk and rewriting the
    header is more robust than asking the model to do arithmetic.
    """
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$")
    lines = diff_text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        m = hunk_re.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        old_start = int(m.group(1))
        new_start = int(m.group(3))
        body = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.startswith((" ", "+", "-", "\\")):
                body.append(line)
                i += 1
                # A line that is genuinely empty must be a context blank,
                # represent it as ' ' to keep the hunk well-formed.
                if line == "":
                    body[-1] = " "
            else:
                break
        old_count = sum(1 for l in body if l.startswith((" ", "-")))
        new_count = sum(1 for l in body if l.startswith((" ", "+")))
        old_hdr = f"{old_start},{old_count}" if old_count != 1 else str(old_start)
        new_hdr = f"{new_start},{new_count}" if new_count != 1 else str(new_start)
        out.append(f"@@ -{old_hdr} +{new_hdr} @@")
        out.extend(body)
    return "\n".join(out)


def apply_patch_and_create_pr(
    repo_name: str,
    file_path: str,
    patched_content: str,
    ref: str = "main",
) -> str:
    """Fork the repo if needed, apply the diff on a new branch, and open a PR.

    Works on any public repository without requiring write access to it.

    Args:
        repo_name: 'owner/repo' of the upstream target on GitHub.
        file_path: the file the diff targets (used for the commit message).
        patched_content: a raw unified diff, as produced by core.analyzer.
        ref: the base branch of the upstream repo to PR against.
    Returns:
        The URL of the opened pull request on the upstream repo.
    """
    if not patched_content.strip():
        raise RuntimeError("Refusing to create a PR from an empty diff")

    gh = Github(config.GITHUB_TOKEN)
    upstream = gh.get_repo(repo_name)
    fork = _ensure_fork(gh, upstream)

    fork_url = (
        f"https://x-access-token:{config.GITHUB_TOKEN}@github.com/{fork.full_name}.git"
    )
    with tempfile.TemporaryDirectory(prefix="patch-pr-") as tmp:
        # Clone the *fork* so push auth works; fetch upstream for the base branch.
        _run(
            ["git", "clone", "--quiet", "--depth", "1",
             f"https://github.com/{upstream.full_name}.git", tmp],
        )
        _run(["git", "remote", "add", "fork", fork_url], cwd=tmp)
        _run(
            ["git", "fetch", "--depth", "1", "origin", ref], cwd=tmp
        )
        _run(["git", "checkout", "--force", "-B", ref, "FETCH_HEAD"], cwd=tmp)

        branch = f"patch/fix-{time.strftime('%Y%m%d-%H%M%S')}"
        _run(["git", "checkout", "-b", branch], cwd=tmp)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False,
            encoding="utf-8", newline="",
        ) as diff_file:
            diff_file.write(
                _fix_hunk_counts(_ensure_diff_header(patched_content, file_path))
                + "\n"
            )
            diff_path = diff_file.name

        try:
            _run(["git", "apply", "--whitespace=nowarn", diff_path], cwd=tmp)
        finally:
            os.unlink(diff_path)

        _run(["git", "add", "-A"], cwd=tmp)
        _run(
            ["git", "-c", "user.name=Patch Bot", "-c", "user.email=patch@bot.local",
             "commit", "-m", f"Fix '{file_path}' (automated patch)"],
            cwd=tmp,
        )
        _run(
            ["git", "push", "--quiet", fork_url, f"{branch}:{branch}"],
            cwd=tmp,
        )

    # Cross-fork PRs must identify the head as 'owner-of-fork:branch'.
    pr = upstream.create_pull(
        title=f"Patch bot: fix '{file_path}'",
        body=(
            f"Automated fix proposal for `{file_path}`,\n"
            f"generated by the Patch pipeline from an issue description.\n\n"
            f"_Submitted via fork `{fork.full_name}`._"
        ),
        head=f"{fork.owner.login}:{branch}",
        base=ref,
    )
    return pr.html_url
