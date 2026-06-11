"""filter_dialogs — local, network-free dialog filtering over the cached list.

Matches Telegram-style exact-ish lookups: title substring (case-insensitive),
username (with/without @, exact or prefix), and id (exact, plus the positive form
of a marked group/channel id). The input is an already-fetched list (the #8 cache),
so filtering never hits the network.
"""

from __future__ import annotations

from tg_messenger.core.models import Dialog
from tg_messenger.core.search import filter_dialogs

_DIALOGS = [
    Dialog(id=7, title="Ann Smith", username="annsmith", kind="dm"),
    Dialog(id=42, title="Bob Jones", username="bobby", kind="dm"),
    Dialog(id=-1001234, title="Dev Team", username="devteam", kind="group"),
    Dialog(id=99, title="Карл", username=None, kind="dm"),
]


def _ids(dialogs):
    return [d.id for d in dialogs]


def test_empty_query_returns_all():
    assert filter_dialogs(_DIALOGS, "") == _DIALOGS
    assert filter_dialogs(_DIALOGS, "   ") == _DIALOGS


def test_title_substring_case_insensitive():
    assert _ids(filter_dialogs(_DIALOGS, "ann")) == [7]
    assert _ids(filter_dialogs(_DIALOGS, "SMITH")) == [7]
    assert _ids(filter_dialogs(_DIALOGS, "карл")) == [99]


def test_username_with_and_without_at():
    assert _ids(filter_dialogs(_DIALOGS, "@bobby")) == [42]
    assert _ids(filter_dialogs(_DIALOGS, "bobby")) == [42]


def test_username_prefix():
    assert _ids(filter_dialogs(_DIALOGS, "@dev")) == [-1001234]


def test_exact_id():
    assert _ids(filter_dialogs(_DIALOGS, "42")) == [42]


def test_marked_id_positive_form():
    # users search "1001234" but the dialog id is the marked -1001234
    assert _ids(filter_dialogs(_DIALOGS, "1001234")) == [-1001234]
    assert _ids(filter_dialogs(_DIALOGS, "-1001234")) == [-1001234]


def test_number_not_an_id_does_not_match_title():
    # "7" is an id (matches dialog 7) but "100" matches nothing by title/id here
    assert filter_dialogs(_DIALOGS, "100500") == []


def test_no_match_returns_empty():
    assert filter_dialogs(_DIALOGS, "zzzznope") == []
