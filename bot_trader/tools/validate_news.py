from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.app.config import load_settings
from bot.app.news import validate_news_calendar


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the trading news calendar")
    parser.add_argument("--settings", default="settings.toml", help="Path to the TOML settings file")
    args = parser.parse_args()

    try:
        settings, _ = load_settings(args.settings)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    calendar_path = Path(args.settings).resolve().parent / settings.news.calendar_file
    try:
        events = validate_news_calendar(calendar_path, min_days_covered=settings.news.min_days_covered)
    except (FileNotFoundError, ValueError) as error:
        print(f"News calendar validation failed: {error}", file=sys.stderr)
        return 2

    print(
        f"News calendar valid: {len(events)} entries, "
        f"symbol coverage={settings.news.min_days_covered}d minimum"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
