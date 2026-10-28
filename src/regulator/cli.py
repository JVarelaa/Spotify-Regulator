"""Command line entry point: `regulator login | check | test-email | run | healthcheck`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import httpx

from .auth import AuthError, TokenManager, TokenStore
from .check import format_results, format_timestamp, run_checks
from .config import Config, ConfigError, load_config, load_dotenv, load_secrets
from .expiry import ExpiryNotices
from .logging_setup import setup_logging
from .login import login
from .notify import NotificationError, build_notifier
from .service import HEARTBEAT_MAX_AGE_S, Service, heartbeat_age
from .spotify import SpotifyClient
from .storage import Storage

log = logging.getLogger(__name__)

REFRESH_EXPIRY_WARNING_S = 21 * 24 * 3600


async def cmd_login(config: Config, args: argparse.Namespace) -> int:
    token = await login(config, load_secrets(), bind_host=args.bind, paste=args.paste)
    print(f"\nLogin correcto. Token guardado en {config.token_path} (permisos 600).")
    print(f"El refresh token caduca hacia el {format_timestamp(token.refresh_expires_at, config.timezone)}; habrá que repetir el login antes.")
    return 0


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    creds = load_secrets()
    async with httpx.AsyncClient(timeout=15) as http:
        tokens = TokenManager(TokenStore(config.token_path), creds.spotify_client_id, http)
        client = SpotifyClient(tokens, http)
        await tokens.access_token()  # fail early with a clear message if there is no usable token
        results = await run_checks(client, write=args.write)
        token = tokens.token

    print(format_results(results))
    if token:
        print(f"\nScopes concedidos: {token.scope}")
        expires = token.refresh_expires_at
        print(f"El refresh token caduca hacia el {format_timestamp(expires, config.timezone)}.")
        if expires - time.time() < REFRESH_EXPIRY_WARNING_S:
            print("Aviso: queda poco; repite `regulator login` pronto.")
    return 0 if all(r.ok is not False for r in results) else 1


async def cmd_test_email(config: Config, args: argparse.Namespace) -> int:
    if config.email is None:
        print("No hay sección email en config.yaml; los avisos solo irían al log.", file=sys.stderr)
        return 1
    notifier = build_notifier(config, load_secrets())
    await notifier.send("Correo de prueba", "Si lees esto, el servicio puede enviarte avisos por correo.")
    print(f"Correo de prueba enviado a {config.email.to_addr}.")
    return 0


async def cmd_run(config: Config, args: argparse.Namespace) -> int:
    creds = load_secrets()
    notifier = build_notifier(config, creds)
    # Docker stops the container with SIGTERM; as PID 1, Python would otherwise ignore it.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    storage = Storage(config.db_path)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            tokens = TokenManager(TokenStore(config.token_path), creds.spotify_client_id, http)
            notices = ExpiryNotices(config.notices_path, notifier, config.timezone, config.spotify.redirect_port)
            service = Service(
                SpotifyClient(tokens, http), tokens, storage, notices, config.polling, config.heartbeat_path
            )
            log.info("service_started", extra={"polling": config.polling.model_dump()})
            try:
                await service.run(stop)
            except ExceptionGroup:
                log.exception("service_crashed")  # exit non-zero: Docker restarts the container
                return 1
    finally:
        storage.close()
    log.info("service_stopped")
    return 0


async def cmd_healthcheck(config: Config, args: argparse.Namespace) -> int:
    age = heartbeat_age(config.heartbeat_path)
    if age is None:
        print("Sin latido: el servicio no ha completado ninguna vuelta.", file=sys.stderr)
        return 1
    if age > HEARTBEAT_MAX_AGE_S:
        print(f"Último latido hace {age:.0f} s: el bucle del reproductor parece colgado.", file=sys.stderr)
        return 1
    return 0


COMMANDS = {
    "login": cmd_login,
    "check": cmd_check,
    "test-email": cmd_test_email,
    "run": cmd_run,
    "healthcheck": cmd_healthcheck,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="regulator", description="Spotify Listening Regulator")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="autoriza la app con tu cuenta de Spotify")
    p_login.add_argument(
        "--bind",
        default=os.environ.get("REGULATOR_CALLBACK_BIND", "127.0.0.1"),
        help="dirección donde escuchar la redirección OAuth (en Docker: 0.0.0.0)",
    )
    p_login.add_argument(
        "--paste", action="store_true", help="no abrir servidor; pegar a mano la URL a la que redirige Spotify"
    )

    p_check = sub.add_parser("check", help="comprueba qué endpoints responden con tu cuenta")
    p_check.add_argument(
        "--write", action="store_true", help="prueba también un comando de escritura inocuo (repeat al valor actual)"
    )

    sub.add_parser("test-email", help="envía un correo de prueba con la configuración de email")
    sub.add_parser("run", help="servicio 24/7: registra escuchas y envía avisos")
    sub.add_parser("healthcheck", help="sale con 0 si el servicio dio una vuelta hace poco (para Docker)")

    args = parser.parse_args(argv)
    load_dotenv(Path(".env"))
    try:
        config = load_config()
        setup_logging(config.log_level)
        return asyncio.run(COMMANDS[args.command](config, args))
    except (ConfigError, AuthError, NotificationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
