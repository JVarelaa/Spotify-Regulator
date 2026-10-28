"""Configuration: non-secret settings from config.yaml, secrets from the environment (.env)."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigError(Exception):
    pass


class SpotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str = "http://127.0.0.1:8888/callback"

    @field_validator("redirect_uri")
    @classmethod
    def _explicit_loopback(cls, value: str) -> str:
        # Spotify rejects "localhost"; it only allows http for explicit loopback IP literals.
        parts = urlsplit(value)
        try:
            is_loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("debe usar una IP de loopback explícita (127.0.0.1 o [::1]), no 'localhost'")
        if parts.port is None:
            raise ValueError("debe incluir el puerto, p. ej. http://127.0.0.1:8888/callback")
        return value

    @property
    def redirect_port(self) -> int:
        return urlsplit(self.redirect_uri).port  # type: ignore[return-value]

    @property
    def redirect_path(self) -> str:
        return urlsplit(self.redirect_uri).path or "/"


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smtp_host: str
    smtp_port: int = 587
    security: Literal["starttls", "ssl"] = "starttls"
    username: str
    from_addr: str | None = None  # defaults to username
    to_addr: str


class PollingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playing_interval_s: float = Field(3.0, gt=0)
    idle_interval_s: float = Field(30.0, gt=0)  # nothing playing, or paused
    recently_played_interval_min: float = Field(60.0, gt=0)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = "Atlantic/Canary"
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    spotify: SpotifyConfig = SpotifyConfig()
    email: EmailConfig | None = None  # without it, notifications only go to the log
    polling: PollingConfig = PollingConfig()

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"zona horaria desconocida: {value}") from None
        return value

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def notices_path(self) -> Path:
        return self.data_dir / "notices.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "regulator.db"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"


class Secrets(BaseModel):
    spotify_client_id: str
    smtp_password: str | None = None


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for running outside Docker; real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("REGULATOR_CONFIG", "config.yaml"))
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    if data_dir := os.environ.get("REGULATOR_DATA_DIR"):
        data["data_dir"] = data_dir
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"configuración inválida en {path}:\n{exc}") from None


def load_secrets() -> Secrets:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        raise ConfigError("SPOTIFY_CLIENT_ID no está definido (ponlo en .env)")
    return Secrets(spotify_client_id=client_id, smtp_password=os.environ.get("SMTP_PASSWORD", "").strip() or None)
