import base64

from github import Github

import config

_github = Github(config.GITHUB_TOKEN)


def get_default_branch(repo_name: str) -> str:
    """Return the repo's default branch (e.g. 'main' or 'master')."""
    return _github.get_repo(repo_name).default_branch


def get_file_content(repo_name: str, file_path: str, ref: str = "main") -> str:
    """Fetch a file from a GitHub repo at the given ref and return it as UTF-8 text.

    On a 404, retries with a fuzzy match from the repo's top-level (and, if
    needed, all) file list: case-insensitive and extension-tolerant, so
    'README.md' can find a file named 'README'.
    """
    repo = _github.get_repo(repo_name)
    try:
        contents = repo.get_contents(file_path, ref=ref)
    except Exception:
        # Fuzzy-resolve: 'path.md' might actually be 'path' (or 'PATH.MD').
        import re
        base = re.sub(r"\.[^./\\]+$", "", file_path.split("/")[-1]).lower()
        candidates = [c.path for c in repo.get_contents("", ref=ref)
                      if c.type != "dir"]
        fuzzy = [p for p in candidates
                 if p.split("/")[-1].lower().startswith(base) or base.startswith(
                     re.sub(r"\.[^./\\]+$", "", p.split("/")[-1]).lower())
                 ]
        if len(fuzzy) == 1:
            contents = repo.get_contents(fuzzy[0], ref=ref)
        elif fuzzy:
            raise ValueError(
                f"Ambiguous file '{file_path}': could be {fuzzy!r}. "
                "Enter the exact path."
            )
        else:
            raise

    # get_contents returns a list when the path is a directory
    if isinstance(contents, list):
        raise ValueError(f"Path '{file_path}' in repo '{repo_name}' is a directory, not a file.")
    if contents.type == "dir":
        raise ValueError(f"Path '{file_path}' in repo '{repo_name}' is a directory, not a file.")

    raw = base64.b64decode(contents.content)
    return raw.decode("utf-8")
