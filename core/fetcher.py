import base64

from github import Github

import config

_github = Github(config.GITHUB_TOKEN)


def get_file_content(repo_name: str, file_path: str, ref: str = "main") -> str:
    """Fetch a file from a GitHub repo at the given ref and return it as UTF-8 text."""
    repo = _github.get_repo(repo_name)
    contents = repo.get_contents(file_path, ref=ref)

    # get_contents returns a list when the path is a directory
    if isinstance(contents, list):
        raise ValueError(f"Path '{file_path}' in repo '{repo_name}' is a directory, not a file.")
    if contents.type == "dir":
        raise ValueError(f"Path '{file_path}' in repo '{repo_name}' is a directory, not a file.")

    raw = base64.b64decode(contents.content)
    return raw.decode("utf-8")
