"""Static assets for the Patch dashboard: the HTML template and its icon."""

from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent          # web/
_PROJECT_ROOT = _PKG_DIR.parent

ICON_PATH = _PROJECT_ROOT / "icon.png"


def load_dashboard_html() -> str:
    """Read the dashboard markup fresh from disk on each call.

    Reading per-request keeps the page live-editable during development
    (no server restart needed to see template changes); the file is small
    (~23 KB) so the IO cost is negligible.
    """
    return (_PKG_DIR / "dashboard.html").read_text(encoding="utf-8")
