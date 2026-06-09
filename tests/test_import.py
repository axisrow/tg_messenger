def test_package_imports_with_version():
    import tg_messenger

    assert isinstance(tg_messenger.__version__, str)
    assert tg_messenger.__version__


def test_public_api_reexported():
    from tg_messenger import (
        Dialog,
        IncomingEvent,
        MediaRef,
        Message,
        StandaloneTelegramClient,
        User,
    )

    assert StandaloneTelegramClient is not None
    assert all(t is not None for t in (Dialog, Message, User, MediaRef, IncomingEvent))
