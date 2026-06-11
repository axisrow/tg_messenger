"""Packaging contract: core + CLI import without dragging the UI stack.

`pip install tg-messenger` (core + CLI) must not require fastapi/uvicorn/textual —
those are optional extras (`[web]`/`[tui]`). The isolation is enforced by running
imports in a FRESH subprocess (the test process already has the UI stack loaded
via the [dev] install, so an in-process `sys.modules` check would be useless) and
asserting the heavy UI modules never landed in that child's `sys.modules`.

The public API snapshot tests pin `__all__` so any change to the exported surface
is a deliberate edit of this test, not an accident.
"""

from __future__ import annotations

import subprocess
import sys

# modules that belong to the optional [web]/[tui]/[interop] extras — importing core
# or the CLI must never pull them in. httpx lives ONLY in interop/ (#20), so it must
# not leak into a plain `import tg_messenger` / `import tg_messenger.cli.main`.
_UI_MODULES = ["fastapi", "uvicorn", "textual", "jinja2", "httpx"]


def _import_loads_no_ui(import_stmt: str) -> str:
    """Run `import_stmt` in a fresh interpreter; return any leaked UI module names."""
    code = (
        f"{import_stmt}\n"
        "import sys\n"
        f"leaked = [m for m in {_UI_MODULES!r} if m in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_importing_core_does_not_load_ui_stack():
    leaked = _import_loads_no_ui("import tg_messenger")
    assert leaked == "", f"importing tg_messenger pulled in UI modules: {leaked}"


def test_importing_cli_does_not_load_ui_stack():
    leaked = _import_loads_no_ui("import tg_messenger.cli.main")
    assert leaked == "", f"importing the CLI pulled in UI modules: {leaked}"


# --- цикл 49: публичный API зафиксирован снапшотом ---

# Editing either list below is a deliberate public-API change, reviewed as a test edit.
_ROOT_ALL = sorted([
    "__version__",
    "StandaloneTelegramClient",
    "Dialog",
    "Message",
    "User",
    "MediaRef",
    "IncomingEvent",
    "SessionStore",
    "LoginFlow",
    "LOGIN_HINT",
    "EventBus",
    "run_with_flood_wait_retry",
    "HandledFloodWaitError",
])

_CORE_ALL = sorted([
    "StandaloneTelegramClient",
    "DeletionWatcher",
    "Dialog",
    "Message",
    "User",
    "MediaRef",
    "IncomingEvent",
    "OutgoingEvent",
    "MessagesDeletedEvent",
    "SessionStore",
    "LoginFlow",
    "LOGIN_HINT",
    "EventBus",
    "run_with_flood_wait_retry",
    "HandledFloodWaitError",
])


def test_root_public_api_snapshot():
    import tg_messenger

    assert sorted(tg_messenger.__all__) == _ROOT_ALL


def test_core_public_api_snapshot():
    import tg_messenger.core as core

    assert sorted(core.__all__) == _CORE_ALL


def test_public_symbols_are_importable_from_root():
    from tg_messenger import (  # noqa: F401
        EventBus,
        HandledFloodWaitError,
        LoginFlow,
        SessionStore,
        run_with_flood_wait_retry,
    )
