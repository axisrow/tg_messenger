"""Contract: read-only-chat capability gating stays wired.

Grep guards (the ``test_stream_consumers.py`` / ``test_packaging.py`` pattern) over
the source text — cheap, no network — so CI fails loudly if someone adds a new send
seam without classifying the rights-rejection errors, or drops the ``can_send`` field
that the UIs use to disable the composer in a chat the account cannot post to.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "tg_messenger"
_CLIENT = (_SRC / "core" / "client.py").read_text(encoding="utf-8")
_MODELS = (_SRC / "core" / "models.py").read_text(encoding="utf-8")
_WEB_APP = (_SRC / "web" / "app.py").read_text(encoding="utf-8")

# Classification is CATEGORY-based (#88): every HTTP-403 ForbiddenError is caught by the
# base class (so a NEW *ForbiddenError is covered with no source change), plus an explicit
# short tuple of the read-only-by-meaning HTTP-400 BadRequestError classes that do NOT
# inherit ForbiddenError and so must be named.
_BADREQUEST_RIGHTS_ERRORS = (
    "ChatAdminRequiredError",
    "UserBannedInChannelError",
    "ChatRestrictedError",
    "VoiceMessagesForbiddenError",
)


def test_client_catches_forbidden_errors_by_category():
    # the base class must be caught — the hand-list of *ForbiddenError was found
    # incomplete three times in #85; category-catch closes that hole for good.
    assert "ForbiddenError" in _CLIENT, "core/client.py must catch ForbiddenError by category"
    assert "except _SEND_FORBIDDEN_ERRORS" in _CLIENT, "send seam must classify rights errors"


def test_client_names_the_badrequest_rights_errors():
    # the 400-category read-only errors have no shared base — they must stay named.
    for name in _BADREQUEST_RIGHTS_ERRORS:
        assert name in _CLIENT, f"{name} (a BadRequest rights error) not referenced in core/client.py"


def test_client_does_not_fold_slowmode_into_read_only():
    # SlowModeWaitError is transient (a FloodError), NOT read-only — it must never be
    # classified as SendForbiddenError. A bare mention in the explanatory comment is fine;
    # what must NOT happen is it being added to the caught tuple.
    assert "SlowModeWaitError" not in _BADREQUEST_RIGHTS_ERRORS


def test_client_defines_send_forbidden_error():
    assert "class SendForbiddenError" in _CLIENT


def test_dialog_model_exposes_can_send():
    assert "can_send" in _MODELS, "Dialog must carry can_send for the UIs to gate the composer"


def test_web_reaction_route_does_not_preflight_can_send():
    # #86: reactions must NOT be re-gated by posting permission. The /reaction route
    # attempts send_reaction directly; a true rights rejection is netted by the global
    # SendForbiddenError handler. Slice the source between the reaction route decorator
    # and the next route (/media) and assert the can_send pre-flight is absent there.
    start = _WEB_APP.index('"/dialogs/{dialog_id}/reaction"')
    rest = _WEB_APP[start:]
    end = rest.index('@app.post("/dialogs/{dialog_id}/media"')
    reaction_route = rest[:end]
    assert "_readonly_error" not in reaction_route, (
        "/reaction must not pre-flight can_send — reactions are not gated by posting permission"
    )


def test_web_has_global_send_forbidden_handler():
    # The authoritative net the removed /reaction pre-flight relies on: a forbidden
    # reaction surfaces here as a clean 403, never a 500.
    assert "@app.exception_handler(SendForbiddenError)" in _WEB_APP
