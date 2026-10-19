import asyncio

import httpx
import pytest

from regulator.spotify import QuotaExceeded, SpotifyClient, SpotifyError, SpotifyUnavailable


class FakeClock:
    """Monotonic clock + sleep that advance together without real waiting."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeTokens:
    def __init__(self):
        self.current = "tok-1"
        self.refreshes = 0

    async def access_token(self) -> str:
        return self.current

    async def force_refresh(self, stale: str) -> None:
        self.refreshes += 1
        self.current = "tok-2"


def run(responder, coro_factory, **client_kwargs):
    """Run `coro_factory(client)` against a MockTransport driven by `responder(request)`."""
    clock = FakeClock()
    tokens = FakeTokens()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            client = SpotifyClient(tokens, http, sleep=clock.sleep, clock=clock, **client_kwargs)
            return await coro_factory(client)

    return asyncio.run(go()), clock, tokens


def test_204_no_active_device_returns_none():
    result, _, _ = run(lambda req: httpx.Response(204), lambda c: c.playback_state())
    assert result is None


def test_playback_requests_episodes_too():
    seen = []

    def responder(req):
        seen.append(req.url.params.get("additional_types"))
        return httpx.Response(200, json={"is_playing": True})

    run(responder, lambda c: c.playback_state())
    assert seen == ["track,episode"]


def test_429_waits_retry_after_then_succeeds():
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1})])
    result, clock, _ = run(lambda req: next(responses), lambda c: c.me())
    assert result == {"ok": 1}
    assert clock.now >= 7


def test_429_backoff_grows_when_retry_after_is_small():
    responses = iter([httpx.Response(429, headers={"Retry-After": "0"})] * 2 + [httpx.Response(200, json={})])
    _, clock, _ = run(lambda req: next(responses), lambda c: c.me())
    assert clock.sleeps == [1.0, 2.0]


def test_gives_up_after_three_attempts_by_default():
    calls = []

    def responder(req):
        calls.append(req)
        return httpx.Response(503)

    with pytest.raises(SpotifyUnavailable):
        run(responder, lambda c: c.me())
    assert len(calls) == 3


def test_quota_exceeded_raises_without_retrying():
    calls = []

    def responder(req):
        calls.append(req)
        return httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            json={"status": 429, "message": "Too many requests", "reason": "QUOTA_EXCEEDED"},
        )

    with pytest.raises(QuotaExceeded) as exc:
        run(responder, lambda c: c.me())
    assert exc.value.retry_after == 3600
    assert len(calls) == 1


def test_quota_exceeded_blocks_following_requests_until_retry_after():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"status": 429, "reason": "QUOTA_EXCEEDED"}}),
            httpx.Response(200, json={}),
        ]
    )

    async def two_calls(client):
        with pytest.raises(QuotaExceeded):
            await client.me()
        return await client.me()

    _, clock, _ = run(lambda req: next(responses), two_calls)
    assert clock.now >= 60


def test_401_refreshes_token_once_and_retries():
    auths = []

    def responder(req):
        auths.append(req.headers["Authorization"])
        return httpx.Response(401) if len(auths) == 1 else httpx.Response(200, json={"id": "me"})

    result, _, tokens = run(responder, lambda c: c.me())
    assert result == {"id": "me"}
    assert tokens.refreshes == 1
    assert auths == ["Bearer tok-1", "Bearer tok-2"]


def test_persistent_401_raises():
    with pytest.raises(SpotifyError) as exc:
        run(lambda req: httpx.Response(401, json={"error": {"status": 401, "message": "bad"}}), lambda c: c.me())
    assert exc.value.status == 401


def test_403_raises_with_message():
    def responder(req):
        return httpx.Response(403, json={"error": {"status": 403, "message": "Player command failed: Premium required"}})

    with pytest.raises(SpotifyError) as exc:
        run(responder, lambda c: c.skip_to_next())
    assert exc.value.status == 403
    assert "Premium" in exc.value.message


def test_network_errors_are_retried_then_give_up():
    def responder(req):
        raise httpx.ConnectError("down")

    with pytest.raises(SpotifyUnavailable):
        run(responder, lambda c: c.me(), max_attempts=3)


def test_5xx_is_retried():
    responses = iter([httpx.Response(502), httpx.Response(200, json={"items": []})])
    result, _, _ = run(lambda req: next(responses), lambda c: c.recently_played(after_ms=123))
    assert result == {"items": []}


def test_recently_played_passes_after_cursor():
    seen = []

    def responder(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"items": []})

    run(responder, lambda c: c.recently_played(after_ms=1700000000000))
    assert seen == [{"limit": "50", "after": "1700000000000"}]


def test_player_commands_never_overlap():
    in_flight = 0
    max_in_flight = 0
    order = []

    async def responder(req):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        order.append(req.url.path)
        in_flight -= 1
        return httpx.Response(204)

    async def concurrent(client):
        await asyncio.gather(client.set_repeat("off"), client.skip_to_next(), client.pause())

    run(responder, concurrent, player_command_gap_s=0)
    assert max_in_flight == 1
    assert order == ["/v1/me/player/repeat", "/v1/me/player/next", "/v1/me/player/pause"]
