def test_package_imports_with_version():
    import tg_messenger

    assert isinstance(tg_messenger.__version__, str)
    assert tg_messenger.__version__


def test_dunder_version_matches_distribution_metadata():
    # __version__ is derived from the installed distribution (#296); a mismatch
    # means the literal drifted from pyproject.toml again. Skipped for bare
    # source checkouts (no installed metadata to compare against).
    from importlib.metadata import PackageNotFoundError, version

    import pytest

    import tg_messenger

    try:
        dist_version = version("tg-messenger")
    except PackageNotFoundError:
        pytest.skip("source checkout: distribution metadata not installed")
    assert tg_messenger.__version__ == dist_version


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
