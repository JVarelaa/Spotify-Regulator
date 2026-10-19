"""Interactive OAuth login: print the authorize URL, catch the redirect, store the token."""

from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qsl, urlsplit

import httpx

from .auth import AuthError, Token, TokenStore, build_authorize_url, exchange_code, make_pkce_pair
from .config import Config, Secrets

CALLBACK_TIMEOUT_S = 300


async def wait_for_callback(bind_host: str, port: int, path: str, timeout: float) -> dict[str, str]:
    """Serve one OAuth redirect on `bind_host:port` and return its query parameters."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, str]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # discard headers
            parts = request_line.decode("latin-1").split()
            target = urlsplit(parts[1] if len(parts) >= 2 else "/")
            if target.path == path:
                if not result.done():
                    result.set_result(dict(parse_qsl(target.query)))
                status, body = "200 OK", "Login recibido. Puedes cerrar esta pestaña y volver a la terminal."
            else:
                status, body = "404 Not Found", "No encontrado."
            payload = body.encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, bind_host, port)
    async with server:
        try:
            return await asyncio.wait_for(result, timeout)
        except TimeoutError:
            raise AuthError(f"no llegó la redirección de Spotify en {timeout:.0f} s") from None


async def login(config: Config, creds: Secrets, *, bind_host: str, paste: bool) -> Token:
    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    redirect_uri = config.spotify.redirect_uri
    url = build_authorize_url(creds.spotify_client_id, redirect_uri, challenge, state)

    print("\n1. Abre esta URL en el navegador de tu portátil e inicia sesión en Spotify:\n")
    print(f"   {url}\n")
    if paste:
        print("2. Spotify te redirigirá a una página que no carga. Copia la URL completa de la barra de direcciones.")
        raw = await asyncio.to_thread(input, "   Pégala aquí: ")
        params = dict(parse_qsl(urlsplit(raw.strip()).query))
    else:
        # flush: when stdout isn't a terminal Python buffers it, and the URL above would never show.
        print(
            f"2. Esperando la redirección en {bind_host}:{config.spotify.redirect_port} (máx. {CALLBACK_TIMEOUT_S // 60} min)...",
            flush=True,
        )
        params = await wait_for_callback(bind_host, config.spotify.redirect_port, config.spotify.redirect_path, CALLBACK_TIMEOUT_S)

    if "error" in params:
        raise AuthError(f"Spotify no concedió la autorización: {params['error']}")
    if params.get("state") != state:
        raise AuthError("el parámetro state no coincide; repite el login desde el principio")
    if not params.get("code"):
        raise AuthError("la redirección no trae parámetro code")

    async with httpx.AsyncClient(timeout=15) as http:
        token = await exchange_code(http, creds.spotify_client_id, redirect_uri, params["code"], verifier)
    if token.missing_scopes:
        raise AuthError(f"Spotify no concedió estos scopes: {', '.join(sorted(token.missing_scopes))}")
    TokenStore(config.token_path).save(token)
    return token
