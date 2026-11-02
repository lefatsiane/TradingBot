from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class VersionInfo:
    commit: str
    clean: bool


def _git_output(arguments: list[str], repository: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"git {' '.join(arguments)} failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def get_version(repository: str | Path | None = None) -> VersionInfo:
    root = Path(repository) if repository is not None else Path(__file__).parents[2]
    commit = _git_output(["rev-parse", "--verify", "HEAD"], root)
    clean = not _git_output(["status", "--porcelain"], root)
    return VersionInfo(commit=commit, clean=clean)
