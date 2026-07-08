"""Behavioral test for the inline chat.html JS handlers via a Node.js harness.

The chat.html script has no browser test harness in this repo; the other web tests grep the
rendered page for expected code. That's too weak for two logic bugs found in #195 review:
  BUG-1 — a network-failed send (xhr.status 0) must NOT run the success path (composer.reset →
          draft wipe); the draft/reply target must survive and an error must show.
  BUG-2 — a stale #messages swap for a dialog the user already left must be dropped, and
          afterSettle must classify search-vs-history per-response (not via a global flag).

tests/js/chat_handlers_test.mjs evaluates the ACTUAL inline script in a minimal DOM sandbox,
dispatches the real handlers the page registered, and asserts on observable state. It exits
non-zero on any failed assertion; this test surfaces its output on failure.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "js" / "chat_handlers_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node.js not available")
def test_chat_html_handlers_behavior():
    result = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # the harness prints "ok/FAIL <name>" per check and exits 1 on any failure
    assert result.returncode == 0, (
        "chat.html handler behavior test failed:\n"
        + result.stdout
        + ("\n[stderr]\n" + result.stderr if result.stderr else "")
    )
