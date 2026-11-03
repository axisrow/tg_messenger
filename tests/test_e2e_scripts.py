"""Structural checks on the manual E2E shell scripts (#272 item 5).

The scripts drive the real installed CLI in a terminal and never run in CI;
these tests only read their text, so they belong in the ordinary offline
suite. They pin the two invariants the QA run violated:

- every `tg delete` is non-interactive (--yes): a bare delete lands on the
  confirm prompt, a non-interactive run hits EOF and aborts, leaving the
  test messages behind;
- step-failure diagnostics never dump raw history (personal data in logs) —
  dumps go through the run-marker redactor.
"""

from __future__ import annotations

from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[1] / "scripts" / "e2e"
SCRIPTS = sorted(E2E_DIR.glob("*.sh"))


def test_every_e2e_delete_is_noninteractive():
    assert SCRIPTS, "e2e scripts not found"
    for script in SCRIPTS:
        for line in script.read_text().splitlines():
            if "tg delete " not in line or line.lstrip().startswith("#"):
                continue
            assert "--yes" in line, f"{script.name}: delete without --yes: {line.strip()}"


def test_e2e_failures_do_not_dump_raw_history():
    assert SCRIPTS, "e2e scripts not found"
    for script in SCRIPTS:
        text = script.read_text()
        assert 'echo "$history"' not in text, f"{script.name}: raw history dump"
        assert 'echo "$after_history"' not in text, f"{script.name}: raw history dump"


def test_history_dump_helper_redacts_to_run_markers():
    lib = E2E_DIR / "lib.sh"
    text = lib.read_text()
    assert "e2e_dump_history" in text, "redacting dump helper missing"
    # the helper itself must grep for this run's marker prefix, not echo raw input
    helper_start = text.index("e2e_dump_history()")
    helper_body = text[helper_start:text.index("\n}", helper_start)]
    assert 'grep -F -- "e2e-$E2E_RUN_ID"' in helper_body
    assert 'echo "$1"' not in helper_body
