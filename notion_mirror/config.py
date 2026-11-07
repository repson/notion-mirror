"""Run configuration: token, paths and options.

Precedence for every setting: CLI flag > real environment variable > the
``.env`` file > built-in default. The ``.env`` file (gitignored) is a plain
KEY=VALUE file, so the tool can be configured without touching the shell
profile; see ``.env.example`` for the full list of variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("notion-mirror")

COMMENTS_ALWAYS = "always"
COMMENTS_ON_CHANGE = "on-change"
COMMENTS_OFF = "off"
COMMENTS_MODES = (COMMENTS_ALWAYS, COMMENTS_ON_CHANGE, COMMENTS_OFF)

# Environment variables (documented in .env.example).
ENV_TOKEN = "NOTION_TOKEN"
ENV_OUTPUT = "NOTION_BACKUP_DIR"
ENV_COMMENTS = "NOTION_COMMENTS"
ENV_REQUESTS_PER_SECOND = "NOTION_REQUESTS_PER_SECOND"

DEFAULT_OUTPUT = "backup"
DEFAULT_REQUESTS_PER_SECOND = 2.5
MIN_REQUESTS_PER_SECOND = 0.1
MAX_REQUESTS_PER_SECOND = 10.0


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` parser (KEY=VALUE lines, no external dependencies)."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'").strip('"')
        if key.strip():
            values[key.strip()] = value
    return values


def load_env(path: Path | None = None) -> dict[str, str]:
    """Local ``.env`` file merged with the real environment.

    Real environment variables win over the file (the usual dotenv behavior).
    """
    merged = load_env_file(path or Path.cwd() / ".env")
    merged.update(os.environ)
    return merged


@dataclass
class Config:
    token: str
    output: Path          # mirror root (e.g. ``backup/``)
    cache: Path           # internal state, gitignored (e.g. ``.cache/``)
    comments: str = COMMENTS_ALWAYS
    force: bool = False
    quiet: bool = False
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND


def from_args(args) -> Config:
    """Builds the configuration from parsed CLI arguments plus the environment."""
    env = load_env()
    token = getattr(args, "token", None) or env.get(ENV_TOKEN, "")

    output = Path(getattr(args, "output", None) or env.get(ENV_OUTPUT) or DEFAULT_OUTPUT)
    if not output.is_absolute():
        output = (Path.cwd() / output).resolve()

    comments = (getattr(args, "comments", None) or env.get(ENV_COMMENTS) or COMMENTS_ALWAYS)
    comments = comments.strip().lower()
    if comments not in COMMENTS_MODES:
        log.warning("Invalid %s=%r; falling back to '%s'",
                    ENV_COMMENTS, comments, COMMENTS_ALWAYS)
        comments = COMMENTS_ALWAYS

    return Config(
        token=token,
        output=output,
        cache=output.parent / ".cache",
        comments=comments,
        force=bool(getattr(args, "force", False)),
        quiet=bool(getattr(args, "quiet", False)),
        requests_per_second=_requests_per_second(env),
    )


def _requests_per_second(env: dict[str, str]) -> float:
    """Parses NOTION_REQUESTS_PER_SECOND with validation and clamping."""
    raw = env.get(ENV_REQUESTS_PER_SECOND)
    if not raw:
        return DEFAULT_REQUESTS_PER_SECOND
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using %.1f",
                    ENV_REQUESTS_PER_SECOND, raw, DEFAULT_REQUESTS_PER_SECOND)
        return DEFAULT_REQUESTS_PER_SECOND
    if not MIN_REQUESTS_PER_SECOND <= value <= MAX_REQUESTS_PER_SECOND:
        log.warning("%s=%r out of range [%.1f, %.1f]; using %.1f",
                    ENV_REQUESTS_PER_SECOND, raw,
                    MIN_REQUESTS_PER_SECOND, MAX_REQUESTS_PER_SECOND,
                    DEFAULT_REQUESTS_PER_SECOND)
        return DEFAULT_REQUESTS_PER_SECOND
    return value
