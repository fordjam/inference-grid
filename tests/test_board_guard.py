from inference_grid.board.guard import check_input


def test_denied_names():
    for name in (
        ".env",
        ".env.local",
        "config/auth.json",
        "credentials.json",
        "id_rsa",
        "server.pem",
        "ledger.sqlite",
        "secrets.yaml",
        "lanes.json",
    ):
        assert check_input(name, b"x") is not None, name
    for name in ("brief.txt", "src/module.py", "tests/test_x.py", "docs/BOARD.md"):
        assert check_input(name, b"plain text") is None, name


def test_secret_patterns_and_binary():
    samples = [
        b"-----BEGIN RSA PRIVATE KEY-----\nabc",
        b"key = sk-abcdefghijklmnopqrstuvwxyz",
        b"Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        b"api_key: 'ZmFrZWtleWZha2VrZXlmYWtla2V5'",
        b"AKIAABCDEFGHIJKLMNOP",
        b"\x00\xff\x00binary",
    ]
    for data in samples:
        assert check_input("notes.txt", data) is not None, data[:20]
    assert check_input("module.py", b'def f():\n    return "api_key is read from a file"\n') is None
    assert check_input("big.txt", b"a" * 2_000_001) is not None
