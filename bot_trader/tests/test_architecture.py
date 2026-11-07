from __future__ import annotations

import ast
from pathlib import Path


def _violations_in(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "MetaTrader5" and path != Path(__file__).parents[1] / "bot" / "adapters" / "mt5_broker.py":
                    violations.append(f"{path.relative_to(Path(__file__).parents[1])}: import MetaTrader5")
                if alias.name in {"sqlite3", "requests"} and path.parts[-3:-1] == ("bot", "core"):
                    violations.append(f"{path.relative_to(Path(__file__).parents[1])}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in {"bot.adapters", "bot.app"} and path.parts[-3:-1] == ("bot", "core"):
                violations.append(f"{path.relative_to(Path(__file__).parents[1])}: import {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
                parent = func.value
                if (
                    path.parts[-3:-1] == ("bot", "core")
                    and name in {"now", "utcnow", "today"}
                    and (
                        isinstance(parent, ast.Name) and parent.id in {"datetime", "date", "time"}
                        or isinstance(parent, ast.Attribute) and parent.attr in {"datetime", "date", "time"}
                    )
                ):
                    violations.append(f"{path.relative_to(Path(__file__).parents[1])}: wall-clock call")
                if (
                    path.parts[-3:-1] == ("bot", "core")
                    and name == "time"
                    and isinstance(parent, ast.Name)
                    and parent.id == "time"
                ):
                    violations.append(f"{path.relative_to(Path(__file__).parents[1])}: wall-clock call")
    return violations


def test_core_and_import_boundaries() -> None:
    bot_root = Path(__file__).parents[1] / "bot"
    violations: list[str] = []
    for path in sorted(bot_root.rglob("*.py")):
        violations.extend(_violations_in(path))

    assert not violations, "\n".join(violations)
