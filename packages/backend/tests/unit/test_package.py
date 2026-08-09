def test_package_exposes_version() -> None:
    import tiny_hermes

    assert tiny_hermes.__version__ == "0.0.0"
