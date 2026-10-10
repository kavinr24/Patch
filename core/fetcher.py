from github import Github

import config

_github = Github(config.GITHUB_TOKEN)


def get_file_content(repo_name: str, file_path: str):
    """Retrieve the content of a single file in the given repo.

    TODO: implement fetch + decode + error handling.
    """
    repo = _github.get_repo(repo_name)
    return repo.get_contents(file_path)
