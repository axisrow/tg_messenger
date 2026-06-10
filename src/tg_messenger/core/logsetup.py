"""Logging setup: rotating file log plus an optional stderr handler.

Every entrypoint calls ``setup_logging`` once; the file always records INFO+
(DEBUG with verbose). The console only shows ERROR+ and skips ``tg_messenger.cli``
records entirely — the CLI talks to the user via click, log noise belongs to the
file. ``--verbose`` lifts both restrictions. The TUI passes ``console=False`` —
stderr would corrupt the alternate screen. Log content policy: operations, ids
and error text — never message bodies or credentials.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path.home() / ".tg_messenger" / "logs"
LOG_FILE_NAME = "tg_messenger.log"

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
    return Path(os.environ.get("TG_LOG_DIR") or DEFAULT_LOG_DIR)


def log_file_path(log_dir: Path | str | None = None) -> Path:
    return _resolve_log_dir(log_dir) / LOG_FILE_NAME


def setup_logging(
    *,
    verbose: bool = False,
    console: bool = True,
    log_dir: Path | str | None = None,
) -> Path:
    """Configure root logging; returns the log file path.

    Idempotent: handlers installed by a previous call are replaced, so the TUI
    can re-run it with ``console=False`` without duplicating output.
    """
    resolved_dir = _resolve_log_dir(log_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(resolved_dir, 0o700)
    log_file = resolved_dir / LOG_FILE_NAME

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
        if not verbose:
            # the CLI already reports its errors via click — no stderr duplicates
            console_handler.addFilter(
                lambda record: not record.name.startswith("tg_messenger.cli")
            )
        setattr(console_handler, _MARKER, True)
        root.addHandler(console_handler)

    # telethon DEBUG floods the log with raw MTProto traffic
    logging.getLogger("telethon").setLevel(logging.DEBUG if verbose else logging.INFO)
    return log_file
