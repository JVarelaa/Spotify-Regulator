import asyncio
import base64
import hashlib
import json
import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from regulator.auth import (
    SCOPES,
    TOKEN_URL,
    ReauthRequired,
    Token,
    TokenEndpointUnavailable,
    TokenManager,
    TokenStore,
    build_authorize_url,
    make_pkce_pair,
)


def make_token(**overrides) -> Token:
    fields = dict(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=1_000.0,
        scope=" ".join(SCOPES),
        authorized_at=500.0,
    )
    fields.update(overrides)
    return Token(**fields)


def token_endpoint(handler):
    """MockTransport that only accepts POSTs to Spotify's token endpoint."""

    def route(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_URL
        return handler(dict(httpx.QueryParams(request.content.decode())))

    return httpx.MockTransport(route)


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = make_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_has_pkce_and_scopes():
    url = build_authorize_url("cid", "http://127.0.0.1:8888/callback", "chal", "st")
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == ["chal"]
    assert q["state"] == ["st"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert set(q["scope"][0].split()) == set(SCOPES)


def test_store_writes_with_mode_600_and_round_trips(tmp_path):
    store = TokenStore(tmp_path / "data" / "token.json")
    store.save(make_token())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load() == make_token()
    assert not store.path.with_suffix(".tmp").exists()


def test_store_load_missing_returns_none(tmp_path):
    assert TokenStore(tmp_path / "nope.json").load() is None


def run_manager(tmp_path, handler, now=2_000.0, token=None):
    store = TokenStore(tmp_path / "token.json")
    store.save(token or make_token())

    async def go():
        async with httpx.AsyncClient(transport=token_endpoint(handler)) as http:
            manager = TokenManager(store, "cid", http, clock=lambda: now)
            return await manager.access_token(), manager

    access, manager = asyncio.run(go())
    return access, store, manager


def test_refresh_persists_rotated_refresh_token(tmp_path):
    def handler(form):
        assert form == {"grant_type": "refresh_token", "refresh_token": "old-refresh", "client_id": "cid"}
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    access, store, _ = run_manager(tmp_path, handler)
    saved = store.load()
    assert access == "new-access"
    assert saved.refresh_token == "new-refresh"
    assert saved.expires_at == 2_000.0 + 3600
    # Rotation must not reset the 6-month clock.
    assert saved.authorized_at == 500.0


def test_refresh_without_new_refresh_token_keeps_old_one(tmp_path):
    access, store, _ = run_manager(
        tmp_path, lambda form: httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})
    )
    assert access == "new-access"
    assert store.load().refresh_token == "old-refresh"


def test_valid_access_token_is_not_refreshed(tmp_path):
    def handler(form):
        raise AssertionError("should not hit the token endpoint")

    access, _, _ = run_manager(tmp_path, handler, now=100.0)
    assert access == "old-access"


def test_invalid_grant_raises_reauth_and_stops_retrying_same_file(tmp_path):
    calls = []

    def handler(form):
        calls.append(form)
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})

    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())

    async def go():
        async with httpx.AsyncClient(transport=token_endpoint(handler)) as http:
            manager = TokenManager(store, "cid", http, clock=lambda: 2_000.0)
            for _ in range(3):
                with pytest.raises(ReauthRequired):
                    await manager.access_token()

    asyncio.run(go())
    assert len(calls) == 1


def test_new_login_revives_manager_after_invalid_grant(tmp_path):
    responses = iter(
        [
            httpx.Response(400, json={"error": "invalid_grant"}),
            httpx.Response(200, json={"access_token": "fresh-access", "expires_in": 3600}),
        ]
    )
    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())

    async def go():
        async with httpx.AsyncClient(transport=token_endpoint(lambda form: next(responses))) as http:
            manager = TokenManager(store, "cid", http, clock=lambda: 2_000.0)
            with pytest.raises(ReauthRequired):
                await manager.access_token()
            # Simulate `regulator login` writing a new file (bump mtime explicitly; same-second writes may tie).
            store.save(make_token(refresh_token="relogin"))
            st = store.path.stat()
            os.utime(store.path, (st.st_atime, st.st_mtime + 10))
            return await manager.access_token()

    assert asyncio.run(go()) == "fresh-access"


def run_with_fake_sleep(store, handler, now=2_000.0):
    """Build a manager whose backoff sleeps are recorded instead of awaited."""
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def go():
        async with httpx.AsyncClient(transport=token_endpoint(handler)) as http:
            manager = TokenManager(store, "cid", http, clock=lambda: now, sleep=fake_sleep)
            return await manager.access_token()

    return go, sleeps


def test_transient_refresh_errors_are_retried(tmp_path):
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(429),
            httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600}),
        ]
    )
    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())
    go, sleeps = run_with_fake_sleep(store, lambda form: next(responses))
    assert asyncio.run(go()) == "new-access"
    assert sleeps == [1.0, 2.0]


def test_network_error_during_refresh_is_retried(tmp_path):
    attempts = []

    def handler(form):
        attempts.append(form)
        if len(attempts) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())
    go, _ = run_with_fake_sleep(store, handler)
    assert asyncio.run(go()) == "new-access"


def test_transient_refresh_errors_give_up_after_three_attempts(tmp_path):
    attempts = []

    def handler(form):
        attempts.append(form)
        return httpx.Response(502)

    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())
    go, _ = run_with_fake_sleep(store, handler)
    with pytest.raises(TokenEndpointUnavailable):
        asyncio.run(go())
    assert len(attempts) == 3
    # A temporary outage must not put the service in "waiting for login" mode.
    assert store.load().refresh_token == "old-refresh"


def test_dead_token_and_new_login_detection(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())

    async def go():
        transport = token_endpoint(lambda form: httpx.Response(400, json={"error": "invalid_grant"}))
        async with httpx.AsyncClient(transport=transport) as http:
            manager = TokenManager(store, "cid", http, clock=lambda: 2_000.0)
            assert manager.dead_token is None
            assert not manager.check_for_new_login()
            with pytest.raises(ReauthRequired):
                await manager.access_token()
            assert manager.dead_token.authorized_at == 500.0
            assert not manager.check_for_new_login()
            store.save(make_token(refresh_token="relogin", authorized_at=9_000.0, expires_at=99_999.0))
            st = store.path.stat()
            os.utime(store.path, (st.st_atime, st.st_mtime + 10))
            assert manager.check_for_new_login()
            assert await manager.access_token() == "old-access"
            assert manager.dead_token is None

    asyncio.run(go())


def test_missing_token_file_raises_reauth(tmp_path):
    async def go():
        async with httpx.AsyncClient() as http:
            await TokenManager(TokenStore(tmp_path / "token.json"), "cid", http).access_token()

    with pytest.raises(ReauthRequired):
        asyncio.run(go())


def test_token_file_is_plain_json(tmp_path):
    store = TokenStore(tmp_path / "token.json")
    store.save(make_token())
    assert json.loads(store.path.read_text())["refresh_token"] == "old-refresh"
