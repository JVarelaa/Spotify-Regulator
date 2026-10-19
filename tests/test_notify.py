import asyncio
import smtplib

import pytest

from regulator.config import Config, ConfigError, EmailConfig, Secrets
from regulator.notify import EmailNotifier, LogNotifier, NotificationError, build_notifier


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None, fail_login=False):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.messages = []
        self.fail_login = fail_login
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg):
        self.calls.append("send")
        self.messages.append(msg)


def email_config(**overrides):
    fields = dict(smtp_host="smtp.example.com", username="me@example.com", to_addr="me@example.com")
    fields.update(overrides)
    return EmailConfig(**fields)


@pytest.fixture(autouse=True)
def reset_instances():
    FakeSMTP.instances.clear()


def test_starttls_flow_and_message():
    notifier = EmailNotifier(email_config(), "app-pass", smtp_factory=FakeSMTP)
    asyncio.run(notifier.send("Hola", "cuerpo"))
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls == ["starttls", "login:me@example.com:app-pass", "send", "quit"]
    msg = smtp.messages[0]
    assert msg["Subject"] == "[Spotify Regulator] Hola"
    assert msg["From"] == "me@example.com"
    assert msg.get_content().strip() == "cuerpo"


def test_ssl_mode_uses_ssl_factory_without_starttls():
    notifier = EmailNotifier(
        email_config(security="ssl", smtp_port=465), "p", smtp_factory=None, smtp_ssl_factory=FakeSMTP
    )
    asyncio.run(notifier.send("s", "b"))
    assert FakeSMTP.instances[0].calls == ["login:me@example.com:p", "send", "quit"]


def test_smtp_errors_become_notification_error():
    def failing(*args, **kwargs):
        return FakeSMTP(*args, **kwargs, fail_login=True)

    notifier = EmailNotifier(email_config(), "wrong", smtp_factory=failing)
    with pytest.raises(NotificationError):
        asyncio.run(notifier.send("s", "b"))


def test_build_notifier_without_email_section_logs_only():
    assert isinstance(build_notifier(Config(), Secrets(spotify_client_id="x")), LogNotifier)


def test_build_notifier_requires_password_when_email_configured():
    with pytest.raises(ConfigError):
        build_notifier(Config(email=email_config()), Secrets(spotify_client_id="x"))
