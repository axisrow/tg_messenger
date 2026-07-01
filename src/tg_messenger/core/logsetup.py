"""Logging setup: rotating file log plus an optional stderr handler.

Every entrypoint calls ``setup_logging`` once; the file always records INFO+
(DEBUG with verbose). The console only shows ERROR+, and a caller that talks to
the user itself (the CLI does, via click) passes its logger prefixes in
``console_skip_prefixes`` so its records never duplicate on stderr.
``--verbose`` lifts both restrictions. The TUI passes ``console=False`` —
stderr would corrupt the alternate screen. Log content policy: operations, ids
and error text — never message bodies or credentials.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tg_messenger.core.names import sanitize_profile_name
from tg_messenger.core.paths import resolve_env_dir, tg_home

LOG_FILE_NAME = "tg_messenger.log"


def default_log_dir() -> Path:
    """``<tg_home>/logs`` — resolved lazily so ``TG_HOME``/legacy state is honored at runtime."""
    return tg_home() / "logs"


def __getattr__(name: str):
    # Back-compat: the old module-level ``DEFAULT_LOG_DIR`` constant is now lazy.
    if name == "DEFAULT_LOG_DIR":
        return default_log_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"
_MARKER = "_tg_messenger_handler"


class _ConsoleFormatter(logging.Formatter):
    """One-line records for stderr; the full traceback lives in the file only."""

    def format(self, record: logging.LogRecord) -> str:
        exc_info, exc_text, stack = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = record.exc_text = record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = exc_info, exc_text, stack


def _resolve_log_dir(log_dir: Path | str | None) -> Path:
    if log_dir is not None:
        return Path(log_dir)
    # TG_LOG_DIR gets the same ~/$VAR expansion + absolute-path fail-closed as TG_HOME
    return resolve_env_dir("TG_LOG_DIR") or default_log_dir()


def _log_file_name(profile: str | None) -> str:
    """``tg_messenger.log`` for the default profile, ``tg_messenger_<profile>.log`` otherwise.

    Per-profile isolation keeps two concurrently running accounts (one process each)
    from interleaving into a single log file.
    """
    safe_profile = sanitize_profile_name(profile) if profile else "default"
    if safe_profile == "default":
        return LOG_FILE_NAME
    return f"tg_messenger_{safe_profile}.log"


def log_file_path(log_dir: Path | str | None = None, *, profile: str | None = None) -> Path:
    return _resolve_log_dir(log_dir) / _log_file_name(profile)


def setup_logging(
    *,
    verbose: bool = False,
    console: bool = True,
    console_skip_prefixes: tuple[str, ...] = (),
    log_dir: Path | str | None = None,
    profile: str | None = None,
) -> Path:
    """Configure root logging; returns the log file path.

    Idempotent: handlers installed by a previous call are replaced, so the TUI
    can re-run it with ``console=False`` without duplicating output. A non-default
    ``profile`` writes to its own ``tg_messenger_<profile>.log``.
    """
    resolved_dir = _resolve_log_dir(log_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_dir, 0o700)
    log_file = resolved_dir / _log_file_name(profile)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    setattr(file_handler, _MARKER, True)
    root.addHandler(file_handler)
    os.chmod(log_file, 0o600)  # may carry sensitive error details, like sessions

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG if verbose else logging.ERROR)
        console_handler.setFormatter(_ConsoleFormatter(_CONSOLE_FORMAT))
        if not verbose and console_skip_prefixes:
            # the caller reports these itself (e.g. CLI via click) — no stderr duplicates
            console_handler.addFilter(
                lambda record: not record.name.startswith(console_skip_prefixes)
            )
        setattr(console_handler, _MARKER, True)
        root.addHandler(console_handler)

    # telethon DEBUG floods the log with raw MTProto traffic
    logging.getLogger("telethon").setLevel(logging.DEBUG if verbose else logging.INFO)
    return log_file
