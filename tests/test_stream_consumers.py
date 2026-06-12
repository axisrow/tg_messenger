"""Contract: every core event stream has a real consumer, and every live UI
consumes the OUTGOING stream.

The core exposes seven ``listen_*`` streams on ``StandaloneTelegramClient``. A
stream that nobody subscribes to is dead code (or, worse, a feature silently
missing from the UIs — exactly the bug this guards: own messages sent from
another device never showed up because no UI consumed ``listen_outgoing()``).

These are grep contracts over the source text (no network, like
``test_packaging.py``): cheap, and they fail loudly in CI when someone adds an
eighth stream and forgets to wire it, or drops ``listen_outgoing`` from a UI.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "tg_messenger"
_CLIENT = _SRC / "core" / "client.py"

_DEFERRED = set()


def _core_stream_names() -> set[str]:
    """All ``async def listen*`` method names defined on the client — the source of truth."""
    text = _CLIENT.read_text(encoding="utf-8")
    return set(re.findall(r"async def (listen\w*)\s*\(", text))


def _consumer_sources() -> dict[Path, str]:
    """Every source file that could consume a stream — the client itself excluded."""
    return {
        p: p.read_text(encoding="utf-8")
        for p in _SRC.rglob("*.py")
        if p != _CLIENT
    }


def test_core_exposes_the_expected_streams():
    # tripwire: if this changes, the contracts below must be revisited deliberately.
    assert _core_stream_names() == {
        "listen",
        "listen_all",
        "listen_outgoing",
        "listen_deleted",
        "listen_chat_actions",
        "listen_reads",
        "listen_reactions",
    }


def test_every_core_stream_has_a_consumer():
    sources = _consumer_sources()
    orphans = []
    for name in _core_stream_names():
        if name in _DEFERRED:
            continue
        # a real consumer calls the stream method somewhere outside client.py
        called = any(re.search(rf"\.{name}\s*\(", text) for text in sources.values())
        if not called:
            orphans.append(name)
    assert not orphans, (
        f"core streams with no consumer: {orphans}. "
        f"Wire them into a UI/service, or add to _DEFERRED with a reason."
    )


def test_live_uis_consume_outgoing():
    """TUI, web and the CLI `chat` REPL must all show our own messages sent elsewhere."""
    must_consume_outgoing = {
        "TUI": _SRC / "tui" / "app.py",
        "web": _SRC / "web" / "app.py",
        "CLI chat": _SRC / "cli" / "main.py",
    }
    missing = [
        label
        for label, path in must_consume_outgoing.items()
        if "listen_outgoing(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"these live UIs never consume listen_outgoing(): {missing}"


def test_live_uis_consume_reactions():
    """TUI, web and the CLI `chat` REPL must all show reaction changes live."""
    must_consume_reactions = {
        "TUI": _SRC / "tui" / "app.py",
        "web": _SRC / "web" / "app.py",
        "CLI chat": _SRC / "cli" / "main.py",
    }
    missing = [
        label
        for label, path in must_consume_reactions.items()
        if "listen_reactions(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"these live UIs never consume listen_reactions(): {missing}"


def test_live_uis_can_send_reactions():
    """TUI, web and CLI must expose the core send_reaction action."""
    must_send_reactions = {
        "TUI": _SRC / "tui" / "app.py",
        "web": _SRC / "web" / "app.py",
        "CLI": _SRC / "cli" / "main.py",
    }
    missing = [
        label
        for label, path in must_send_reactions.items()
        if ".send_reaction(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"these UIs never call send_reaction(): {missing}"
