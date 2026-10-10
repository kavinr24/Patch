import os

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def _require(name: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


GROQ_API_KEY = _require("GROQ_API_KEY", GROQ_API_KEY)
GITHUB_TOKEN = _require("GITHUB_TOKEN", GITHUB_TOKEN)
