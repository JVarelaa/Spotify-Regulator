"""Notification channels behind one interface. Email (SMTP) for now; ntfy can be added later."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Protocol

from .config import Config, ConfigError, EmailConfig, Secrets

log = logging.getLogger(__name__)

SUBJECT_PREFIX = "[Spotify Regulator]"


class NotificationError(Exception):
    pass


class Notifier(Protocol):
    async def send(self, subject: str, body: str) -> None: ...


class LogNotifier:
    """Fallback when no channel is configured: the notice only shows up in the logs."""

    async def send(self, subject: str, body: str) -> None:
        log.warning("notification", extra={"subject": subject, "body": body})


class EmailNotifier:
    def __init__(
        self,
        cfg: EmailConfig,
        password: str,
        *,
        timeout_s: float = 30.0,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
    ):
        self._cfg = cfg
        self._password = password
        self._timeout = timeout_s
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory

    async def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"{SUBJECT_PREFIX} {subject}"
        msg["From"] = self._cfg.from_addr or self._cfg.username
        msg["To"] = self._cfg.to_addr
        msg.set_content(body)
        try:
            # smtplib is blocking; run it in a thread so the polling loop keeps going.
            await asyncio.to_thread(self._deliver, msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(f"no se pudo enviar el correo: {exc}") from exc
        log.info("email_sent", extra={"subject": subject})

    def _deliver(self, msg: EmailMessage) -> None:
        cfg = self._cfg
        context = ssl.create_default_context()
        if cfg.security == "ssl":
            smtp = self._smtp_ssl_factory(cfg.smtp_host, cfg.smtp_port, timeout=self._timeout, context=context)
        else:
            smtp = self._smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=self._timeout)
        with smtp:
            if cfg.security == "starttls":
                smtp.starttls(context=context)
            smtp.login(cfg.username, self._password)
            smtp.send_message(msg)


def build_notifier(config: Config, creds: Secrets) -> Notifier:
    if config.email is None:
        return LogNotifier()
    if not creds.smtp_password:
        raise ConfigError("config.yaml tiene sección email pero falta SMTP_PASSWORD en .env")
    return EmailNotifier(config.email, creds.smtp_password)
