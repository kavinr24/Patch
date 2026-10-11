import os
import sys

from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def _require(name: str, value: str | None) -> str:
    if not value:
        sys.exit(
            f"\n[config] FATAL: environment variable '{name}' is not set.\n"
            f"  Local dev : put it in a .env file (see .env.example).\n"
            f"  Render    : add '{name}' under Environment in the service settings,\n"
            f"              then manually deploy to pick it up.\n"
        )
    if not value.strip():
        sys.exit(f"\n[config] FATAL: environment variable '{name}' is set but blank.\n")
    return value


GITHUB_TOKEN = _require("GITHUB_TOKEN", GITHUB_TOKEN)
GROQ_API_KEY = _require("GROQ_API_KEY", GROQ_API_KEY)


def _mask(value: str) -> str:
    return f"{value[:4]}…{value[-3:]}" if len(value) > 10 else "…"


print(
    f"[config] ok: GITHUB_TOKEN={_mask(GITHUB_TOKEN)} "
    f"GROQ_API_KEY={_mask(GROQ_API_KEY)}"
)
