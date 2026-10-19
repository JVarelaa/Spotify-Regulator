import pytest

from regulator.config import ConfigError, load_config


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.delenv("REGULATOR_DATA_DIR", raising=False)
    config = load_config(tmp_path / "missing.yaml")
    assert config.timezone == "Atlantic/Canary"
    assert config.spotify.redirect_port == 8888
    assert config.spotify.redirect_path == "/callback"


@pytest.mark.parametrize(
    "uri",
    ["http://localhost:8888/callback", "http://192.168.1.10:8888/callback", "http://127.0.0.1/callback"],
)
def test_redirect_uri_must_be_loopback_ip_with_port(tmp_path, uri):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"spotify:\n  redirect_uri: {uri}\n"))


def test_ipv6_loopback_accepted(tmp_path):
    config = load_config(write(tmp_path, "spotify:\n  redirect_uri: http://[::1]:9000/cb\n"))
    assert config.spotify.redirect_port == 9000


def test_unknown_keys_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "timezon: Europe/Madrid\n"))


def test_data_dir_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REGULATOR_DATA_DIR", "/data")
    config = load_config(write(tmp_path, "data_dir: ./elsewhere\n"))
    assert str(config.token_path) == "/data/token.json"
