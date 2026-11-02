from __future__ import annotations

import argparse
import sys

from bot.app.config import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading bot development utilities")
    parser.add_argument("--settings", default="settings.toml")
    parser.add_argument(
        "command",
        choices=("validate",),
        help="validate configuration only; this baseline does not connect to MT5 or place orders",
    )
    args = parser.parse_args()
    try:
        settings, version = load_settings(args.settings)
    except (OSError, ValueError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
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
