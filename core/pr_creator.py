"""Pull request creation from applied patches."""

import github


def apply_patch_and_create_pr(
    repo_name: str,
    file_path: str,
    patched_content: str,
    ref: str = "main",
) -> str:
    """Apply a patched file to a new branch and create a pull request.

    TODO: implement branch creation, commit and PR creation via PyGithub.
    """
    raise NotImplementedError("apply_patch_and_create_pr is not implemented yet")
