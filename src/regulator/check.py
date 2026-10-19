"""`regulator check`: probe every endpoint the project relies on with the stored token."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .spotify import QuotaExceeded, SpotifyClient, SpotifyError, SpotifyUnavailable


@dataclass
class CheckResult:
    name: str
    endpoint: str
    ok: bool | None  # None = skipped
    detail: str


async def run_checks(client: SpotifyClient, *, write: bool) -> list[CheckResult]:
    results: list[CheckResult] = []

    async def probe(name: str, endpoint: str, call: Callable[[], Awaitable[Any]], describe: Callable[[Any], str]) -> Any:
        try:
            data = await call()
        except QuotaExceeded as exc:
            results.append(CheckResult(name, endpoint, False, f"cuota de Development Mode agotada; reintenta en {exc.retry_after:.0f} s"))
        except SpotifyError as exc:
            hint = " (endpoint no disponible para esta app, o tu usuario no está en User Management)" if exc.status == 403 else ""
            results.append(CheckResult(name, endpoint, False, f"{exc}{hint}"))
        except SpotifyUnavailable as exc:
            results.append(CheckResult(name, endpoint, False, str(exc)))
        else:
            results.append(CheckResult(name, endpoint, True, describe(data)))
            return data
        return None

    await probe("Perfil", "GET /me", client.me, _describe_me)
    playback = await probe("Estado del reproductor", "GET /me/player", client.playback_state, _describe_playback)
    await probe("Pista actual", "GET /me/player/currently-playing", client.currently_playing, _describe_playback)
    await probe("Dispositivos", "GET /me/player/devices", client.devices, _describe_devices)
    await probe(
        "Historial reciente", "GET /me/player/recently-played", lambda: client.recently_played(limit=5), _describe_recent
    )

    repeat_endpoint = "PUT /me/player/repeat"
    device = (playback or {}).get("device") or {}
    if not write:
        results.append(CheckResult("Escritura (repeat)", repeat_endpoint, None, "omitido; usa --write para probarlo"))
    elif not playback or device.get("is_restricted") or not playback.get("repeat_state"):
        results.append(
            CheckResult("Escritura (repeat)", repeat_endpoint, None, "omitido; abre Spotify en algún dispositivo y dale al play")
        )
    else:
        state = playback["repeat_state"]
        await probe(
            "Escritura (repeat)",
            repeat_endpoint,
            lambda: client.set_repeat(state, device.get("id")),
            lambda _: f"OK (lo puse al mismo valor que tenía: {state}, sin cambios)",
        )
    for name, endpoint in (("Saltar pista", "POST /me/player/next"), ("Pausa", "PUT /me/player/pause")):
        results.append(
            CheckResult(name, endpoint, None, "no se prueba (interrumpe lo que suena); mismo scope y requisito Premium que repeat")
        )
    return results


def _describe_me(data: dict | None) -> str:
    data = data or {}
    return f"usuario {data.get('display_name') or data.get('id', '?')}"


def _describe_playback(data: dict | None) -> str:
    if not data:
        return "OK, nada sonando o sin dispositivo activo (204)"
    kind = data.get("currently_playing_type", "?")
    item = data.get("item") or {}
    if kind == "track" and item:
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        what = f"{artists} - {item.get('name')}"
    elif kind == "episode" and item:
        what = f"episodio: {item.get('name')}"
    else:
        what = kind
    extra = []
    if device := data.get("device"):
        extra.append(f"en {device.get('name')}")
    if "repeat_state" in data:
        extra.append(f"repeat={data['repeat_state']} shuffle={data.get('shuffle_state')}")
    state = "sonando" if data.get("is_playing") else "en pausa"
    return f"OK, {state}: {what}" + (f" ({', '.join(extra)})" if extra else "")


def _describe_devices(data: dict | None) -> str:
    devices = (data or {}).get("devices", [])
    if not devices:
        return "OK, ningún dispositivo visible ahora mismo"
    return "OK, " + ", ".join(f"{d.get('name')}{' (activo)' if d.get('is_active') else ''}" for d in devices)


def _describe_recent(data: dict | None) -> str:
    items = (data or {}).get("items", [])
    if not items:
        return "OK, historial vacío"
    last = items[0]
    track = last.get("track") or {}
    return f"OK, {len(items)} entradas; la última es {track.get('name')} ({last.get('played_at')})"


def format_results(results: list[CheckResult]) -> str:
    marks = {True: "OK  ", False: "FALLO", None: "--  "}
    width = max(len(r.endpoint) for r in results)
    return "\n".join(f"[{marks[r.ok]}] {r.endpoint.ljust(width)}  {r.detail}" for r in results)


def format_timestamp(epoch_s: float, tz: str) -> str:
    return datetime.fromtimestamp(epoch_s, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
