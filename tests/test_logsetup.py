import logging
import logging.handlers
import stat
import sys

import pytest

from tg_messenger.core.logsetup import LOG_FILE_NAME, setup_logging

_MARKER = "_tg_messenger_handler"


def _marked_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, _MARKER, False)]


def _console_handlers():
    return [
        h for h in _marked_handlers()
        if not isinstance(h, logging.handlers.RotatingFileHandler)
    ]


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    old_level = root.level
    old_telethon = logging.getLogger("telethon").level
    yield
    for h in _marked_handlers():
        root.removeHandler(h)
        h.close()
    root.setLevel(old_level)
    logging.getLogger("telethon").setLevel(old_telethon)


def test_creates_log_file_and_records_messages(tmp_path):
    log_file = setup_logging(log_dir=tmp_path / "logs")
    logging.getLogger("tg_messenger.test").warning("hello-marker")
    assert log_file.name == LOG_FILE_NAME
    assert "hello-marker" in log_file.read_text(encoding="utf-8")


def test_info_recorded_in_file_by_default(tmp_path):
    log_file = setup_logging(log_dir=tmp_path / "logs")
    logging.getLogger("tg_messenger.test").info("info-marker")
    assert "info-marker" in log_file.read_text(encoding="utf-8")


def test_debug_needs_verbose(tmp_path):
    log_file = setup_logging(log_dir=tmp_path / "logs")
    logging.getLogger("tg_messenger.test").debug("debug-marker")
    assert "debug-marker" not in log_file.read_text(encoding="utf-8")

    log_file = setup_logging(verbose=True, log_dir=tmp_path / "logs")
    logging.getLogger("tg_messenger.test").debug("debug-marker")
    assert "debug-marker" in log_file.read_text(encoding="utf-8")


def test_log_dir_and_file_are_private(tmp_path):
    log_dir = tmp_path / "logs"
    log_file = setup_logging(log_dir=log_dir)
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_repeated_setup_does_not_duplicate_handlers(tmp_path):
    setup_logging(log_dir=tmp_path / "logs")
    first = len(_marked_handlers())
    setup_logging(log_dir=tmp_path / "logs")
    assert len(_marked_handlers()) == first


def test_console_handler_warning_by_default(tmp_path):
    setup_logging(log_dir=tmp_path / "logs")
    handlers = _console_handlers()
    assert len(handlers) == 1
    assert handlers[0].level == logging.WARNING
    assert handlers[0].stream is sys.stderr


def test_console_handler_debug_when_verbose(tmp_path):
    setup_logging(verbose=True, log_dir=tmp_path / "logs")
    assert _console_handlers()[0].level == logging.DEBUG


def test_console_off_installs_no_stderr_handler(tmp_path):
    setup_logging(console=False, log_dir=tmp_path / "logs")
    assert _console_handlers() == []


def test_telethon_logger_capped_at_info_unless_verbose(tmp_path):
    setup_logging(log_dir=tmp_path / "logs")
    assert logging.getLogger("telethon").level == logging.INFO
    setup_logging(verbose=True, log_dir=tmp_path / "logs")
    assert logging.getLogger("telethon").level == logging.DEBUG


def test_console_gets_one_line_file_gets_traceback(tmp_path, capsys):
    log_file = setup_logging(log_dir=tmp_path / "logs")
    try:
        raise RuntimeError("boom-marker")
    except RuntimeError:
        logging.getLogger("tg_messenger.test").exception("operation failed")
    err = capsys.readouterr().err
    assert "operation failed" in err
    assert "Traceback" not in err  # console stays friendly
    content = log_file.read_text(encoding="utf-8")
    assert "Traceback" in content
    assert "boom-marker" in content


def test_env_var_overrides_default_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_LOG_DIR", str(tmp_path / "envlogs"))
    log_file = setup_logging()
    assert log_file.parent == tmp_path / "envlogs"
