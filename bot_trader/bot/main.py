from __future__ import annotations

import argparse
import sys

from bot.app.config import load_settings, schedule_validation_failures
from bot.app.runtime import run_bot


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo-only Trading Bot commands")
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument(
        "command",
        choices=("validate", "run"),
        help="validate configuration, or run the demo-only broker engine",
    )
    args = parser.parse_args()
    if args.command == "run":
        try:
            run_bot(args.settings)
        except (OSError, PermissionError, RuntimeError, ValueError) as error:
            print(f"Runtime error: {error}", file=sys.stderr)
            return 2
        return 0

    try:
        settings, version = load_settings(args.settings)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    schedule_failures = schedule_validation_failures(settings)
    if schedule_failures:
        print("Schedule validation failed:", file=sys.stderr)
        for failure in schedule_failures:
            print(f"- {failure}", file=sys.stderr)
        return 2
    print(
        f"Configuration valid: mode={settings.account.mode}, "
        f"enabled_symbols={sum(symbol.enabled for symbol in settings.symbols.values())}, "
        f"settings_sha256={version}"
    )
    print("Safety: configuration validation only; no terminal connection or orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
