import os
import re

from dotenv import load_dotenv

load_dotenv()

from groq import Groq

import config

# Groq uses an OpenAI-compatible API surface, so `groq.Groq` exposes the same
# client.chat.completions.create(...) interface as the OpenAI SDK.
_groq = Groq(api_key=config.GROQ_API_KEY)

_MODEL = "qwen/qwen3.8-27b"

# Free-tier Groq enforces output-tokens-per-minute (OTPM) limits (e.g. 1000
# on qwen3.8-27b); keep the default below it and allow override via env.
try:
    _MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "900"))
except (TypeError, ValueError):
    _MAX_TOKENS = 900

_SYSTEM_PROMPT = (
    "You are an expert software engineer. You receive the current contents of a "
    "single source file and a description of a bug or issue in that file. "
    "Reply ONLY with a valid code diff (unified diff format for that file) that "
    "fixes the described issue. No prose, no explanations, no markdown fences -- "
    "just the diff."
)


def generate_patch(file_path: str, file_content: str, error_context: str) -> str:
    """Ask Groq's LLM for a patch fixing the described issue in the given file.

    Returns the raw model output (a unified diff) as a string.
    Raises RuntimeError on API failure.
    """
    user_prompt = (
        f"File path: {file_path}\n\n"
        f"Current file content:\n'''\n{file_content}\n'''\n\n"
        f"Issue / error details:\n{error_context}\n"
    )

    try:
        response = _groq.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=_MAX_TOKENS,
        )
    except Exception as exc:
        raise RuntimeError(f"Groq API call failed: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(
            f"Groq returned an empty response (finish_reason="
            f"{response.choices[0].finish_reason!r}); increase max_tokens or retry."
        )
    return _sanitize_diff(content.strip())


def _sanitize_diff(raw: str) -> str:
    """Strip markdown code-block fences, leaving only the raw unified diff.

    Models sometimes wrap output in ```diff ... ``` despite prompt
    instructions. Removes the fences (and any language tag) and surrounding
    whitespace so the result can be fed to `git apply` directly.
    """
    text = raw.strip()
    fence_pattern = re.compile(
        r"^```[a-zA-Z]*\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL
    )
    match = fence_pattern.match(text)
    if match:
        text = match.group("body").strip()

    # Defensive: a stray leading fence line (e.g. ```diff) with a malformed
    # or missing closing fence would still slip past the full-block match.
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "````":
        lines = lines[:-1]
    return "\n".join(lines).strip()
