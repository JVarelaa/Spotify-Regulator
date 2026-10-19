import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from regulator.auth import REFRESH_TOKEN_LIFETIME_S, SCOPES, Token
from regulator.expiry import ExpiryNotices, due_warning
from regulator.notify import NotificationError

TZ = "Atlantic/Canary"
EXPIRY = date(2027, 4, 20)


@pytest.mark.parametrize(
    ("today", "sent", "expected"),
    [
        (date(2027, 4, 10), [], None),  # too early
        (date(2027, 4, 17), [], 3),
        (date(2027, 4, 17), [3], None),  # already sent
        (date(2027, 4, 18), [3], 2),
        (date(2027, 4, 19), [3, 2], None),  # nothing scheduled 1 day before
        (date(2027, 4, 20), [3, 2], 0),
        (date(2027, 4, 20), [3, 2, 0], None),
        (date(2027, 4, 21), [], None),  # past expiry: the reauth notice takes over
        # After downtime, only the most recent warning, not a burst of stale ones.
        (date(2027, 4, 18), [], 2),
        (date(2027, 4, 19), [], 2),
        (date(2027, 4, 20), [], 0),
        (date(2027, 4, 20), [3], 0),
    ],
)
def test_due_warning(today, sent, expected):
    assert due_warning(today, EXPIRY, sent) == expected


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def send(self, subject, body):
        if self.fail:
            raise NotificationError("smtp down")
        self.sent.append((subject, body))


def token_expiring_on(day: date, **overrides) -> Token:
    expires = datetime(day.year, day.month, day.day, 12, 0, tzinfo=ZoneInfo(TZ)).timestamp()
    fields = dict(
        access_token="a",
        refresh_token="r",
        expires_at=0.0,
        scope=" ".join(SCOPES),
        authorized_at=expires - REFRESH_TOKEN_LIFETIME_S,
    )
    fields.update(overrides)
    return Token(**fields)


def at(day: date, hour: int = 10) -> float:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=ZoneInfo(TZ)).timestamp()


class Harness:
    def __init__(self, tmp_path, notifier=None):
        self.path = tmp_path / "notices.json"
        self.notifier = notifier or FakeNotifier()
        self.now = 0.0
        self.notices = self.fresh()

    def fresh(self):
        """A new ExpiryNotices over the same state file, as after a service restart."""
        return ExpiryNotices(self.path, self.notifier, TZ, 8888, clock=lambda: self.now)

    def check(self, now, token=None, dead=None):
        self.now = now
        asyncio.run(self.notices.check(token, dead))


def test_sends_each_warning_once_across_ticks_and_restarts(tmp_path):
    h = Harness(tmp_path)
    token = token_expiring_on(EXPIRY)
    h.check(at(date(2027, 4, 17)), token)
    h.check(at(date(2027, 4, 17), 11), token)
    h.notices = h.fresh()
    h.check(at(date(2027, 4, 17), 12), token)
    assert [s for s, _ in h.notifier.sent] == ["El login de Spotify caduca en 3 días"]

    h.check(at(date(2027, 4, 18)), token)
    h.check(at(date(2027, 4, 20)), token)
    assert [s for s, _ in h.notifier.sent] == [
        "El login de Spotify caduca en 3 días",
        "El login de Spotify caduca en 2 días",
        "El login de Spotify caduca hoy",
    ]
    assert "ssh -L 8888:127.0.0.1:8888" in h.notifier.sent[0][1]


def test_new_login_resets_warnings(tmp_path):
    h = Harness(tmp_path)
    h.check(at(date(2027, 4, 17)), token_expiring_on(EXPIRY))
    later = date(2027, 10, 17)
    h.check(at(date(2027, 10, 14)), token_expiring_on(later))
    assert len(h.notifier.sent) == 2


def test_reauth_notice_sent_once_per_dead_token(tmp_path):
    h = Harness(tmp_path)
    dead = token_expiring_on(EXPIRY)
    h.check(at(date(2027, 4, 21)), dead=dead)
    h.check(at(date(2027, 4, 21), 11), dead=dead)
    h.notices = h.fresh()
    h.check(at(date(2027, 4, 22)), dead=dead)
    assert [s for s, _ in h.notifier.sent] == ["El servicio ha perdido el acceso a Spotify"]


def test_failed_send_is_retried_later_not_every_tick(tmp_path):
    notifier = FakeNotifier(fail=True)
    h = Harness(tmp_path, notifier)
    token = token_expiring_on(EXPIRY)
    start = at(date(2027, 4, 17))
    h.check(start, token)
    notifier.fail = False
    h.check(start + 60, token)  # still inside the retry delay
    assert notifier.sent == []
    h.check(start + 16 * 60, token)
    assert [s for s, _ in notifier.sent] == ["El login de Spotify caduca en 3 días"]
