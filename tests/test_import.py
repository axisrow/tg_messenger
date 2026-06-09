def test_package_imports_with_version():
    import tg_messenger

    assert isinstance(tg_messenger.__version__, str)
    assert tg_messenger.__version__
