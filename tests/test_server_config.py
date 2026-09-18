import pytest

import app.server as server
from app.server_config import ServerSettings


@pytest.fixture(autouse=True)
def clean_binding(monkeypatch):
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)


def test_defaults():
    settings = ServerSettings.from_env()
    assert (settings.host, settings.port, settings.local_url) == ("0.0.0.0", 8008, "http://127.0.0.1:8008")


@pytest.mark.parametrize("host, expected", [
    ("127.0.0.1", "http://127.0.0.1:9009"),
    ("192.168.1.20", "http://192.168.1.20:9009"),
    ("localhost", "http://localhost:9009"),
    ("::", "http://[::1]:9009"),
    ("::1", "http://[::1]:9009"),
    ("2001:db8::1", "http://[2001:db8::1]:9009"),
])
def test_custom_binding_and_client_url(monkeypatch, host, expected):
    monkeypatch.setenv("HOST", host)
    monkeypatch.setenv("PORT", "9009")
    settings = ServerSettings.from_env()
    assert settings.local_url == expected
    assert settings.host == host


@pytest.mark.parametrize("key, value", [
    ("PORT", "0"), ("PORT", "65536"), ("PORT", "eight"), ("PORT", "8008.0"), ("PORT", ""),
    ("HOST", ""), ("HOST", "http://127.0.0.1"), ("HOST", "127.0.0.1:9009"),
    ("HOST", "bad host"), ("HOST", "localhost/path"),
])
def test_invalid_configuration(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        ServerSettings.from_env()


def test_server_loads_env_and_honors_process_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    env_file = tmp_path / ".env"
    content = "HOST=127.0.0.1\nPORT=9009\n"
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("PORT", "9010")
    # Track keys loaded by dotenv so this test restores the surrounding process.
    monkeypatch.setenv("HOST", "temporary")
    monkeypatch.delenv("HOST")
    calls = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert server.main() == 0
    assert calls == [("app.main:app", {"host": "127.0.0.1", "port": 9010})]
    assert "http://127.0.0.1:9010" in capsys.readouterr().out
    assert env_file.read_text(encoding="utf-8") == content


def test_invalid_port_never_starts_listener(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("PORT", "bad")
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **kw: pytest.fail("invalid listener started"))
    assert server.main() == 1
    assert "PORT" in capsys.readouterr().err
