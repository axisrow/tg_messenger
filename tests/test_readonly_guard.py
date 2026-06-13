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

# the Telegram rights-rejection errors a send may raise — all must be classified.
# NOTE: SlowModeWaitError is deliberately NOT here — it's a transient wait, not a
# read-only state, so it must NOT be folded into SendForbiddenError.
_RIGHTS_ERRORS = (
    "ChatAdminRequiredError",
    "ChatWriteForbiddenError",
    "ChatSendMediaForbiddenError",
    "UserBannedInChannelError",
    "ChatGuestSendForbiddenError",
    "ChatRestrictedError",
    "ChatSendGifsForbiddenError",
    "ChatSendStickersForbiddenError",
    "ChatSendPollForbiddenError",
    "VoiceMessagesForbiddenError",
)


def test_client_classifies_every_rights_error():
    for name in _RIGHTS_ERRORS:
        assert name in _CLIENT, f"{name} not referenced in core/client.py — send seam unguarded"


def test_client_defines_send_forbidden_error():
    assert "class SendForbiddenError" in _CLIENT


def test_dialog_model_exposes_can_send():
    assert "can_send" in _MODELS, "Dialog must carry can_send for the UIs to gate the composer"
