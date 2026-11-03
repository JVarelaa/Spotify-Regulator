# Spotify Listening Regulator

Servicio personal que regula cómo escuchas música en Spotify. La especificación completa está en
[spotify-regulator-spec.md](spotify-regulator-spec.md).

Estado: **fase 1** (autorización, comprobación de endpoints y avisos de caducidad del login).
El servicio 24/7 (`regulator run`) llega en la fase 2.

## 1. Crear la app en Spotify

1. Entra en <https://developer.spotify.com/dashboard> con tu cuenta (necesita **Premium**:
   Development Mode lo exige desde marzo de 2026).
2. Crea una app y, en *Redirect URIs*, añade exactamente `http://127.0.0.1:8888/callback`.
   Spotify no admite `localhost`, solo la IP de loopback.
3. Marca *Web API* como API usada.
4. Copia el *Client ID*. El client secret no hace falta (usamos PKCE).
5. Si `regulator check` devuelve 403 en todo, añade tu email en *User Management*.

## 2. Configuración

```sh
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env
```

- `.env`: `SPOTIFY_CLIENT_ID` y, si quieres avisos por correo, `SMTP_PASSWORD`.
- `config.yaml`: zona horaria, redirect URI y la sección `email`.

### Correo (Gmail)

1. Activa la verificación en dos pasos en tu cuenta de Google.
2. Crea una *contraseña de aplicación* (Cuenta de Google → Seguridad → Contraseñas de aplicaciones).
   Solo sirve para enviar correo, no para entrar en tu cuenta, y la puedes revocar cuando quieras.
3. Ponla en `.env` como `SMTP_PASSWORD` y rellena `email` en `config.yaml`.
4. Comprueba que funciona: `regulator test-email`.

## 3. Login (una vez cada ~6 meses)

Spotify hace caducar el acceso 6 meses después de cada login. El servicio te avisa por correo
3 días antes, 2 días antes y el mismo día. Si llega a caducar, sigue en marcha sin hacer
peticiones y vuelve a funcionar solo en cuanto detecta el token nuevo.

El servidor no tiene navegador, así que el login se hace desde el portátil a través de un
**túnel SSH**: Spotify redirige tu navegador a `127.0.0.1:8888` (tu portátil), y el túnel lleva
esa conexión al puerto 8888 del servidor, donde espera `regulator login`.

En el homelab, la primera vez:

```sh
mkdir -p data && sudo chown 1000:1000 data   # el contenedor corre con uid 1000
docker compose build
```

Desde el portátil:

```sh
ssh -L 8888:127.0.0.1:8888 usuario@homelab
```

Y en esa misma sesión SSH, dentro del directorio del proyecto:

```sh
docker compose run --rm -p 127.0.0.1:8888:8888 regulator login
```

Abre en el navegador **del portátil** la URL que imprime, acepta, y el token queda guardado en
`data/token.json` con permisos 600.

Si el túnel da problemas: `docker compose run --rm regulator login --paste` (sin `-p`). Tras
aceptar, el navegador mostrará un error de conexión; copia la URL de la barra de direcciones y
pégala en la terminal.

## 4. Comprobar los endpoints

```sh
docker compose run --rm regulator check           # solo lecturas
docker compose run --rm regulator check --write   # + pone repeat al valor que ya tenía (inocuo)
```

`--write` necesita que haya algo sonando en algún dispositivo. Saltar pista y pausar no se
prueban porque interrumpirían la reproducción.

## 5. Arrancar el servicio

```sh
docker compose up -d
docker compose logs -f   # logs en JSON, una línea por evento
docker compose ps        # STATUS muestra (healthy) mientras el bucle del reproductor da vueltas
```

Consulta el reproductor cada 3 s mientras suena música y cada 30 s si no suena nada,
sincroniza `recently-played` al arrancar y cada hora, y revisa los avisos de caducidad cada
minuto. Los intervalos están en la sección `polling` de `config.yaml`.

Para actualizarlo: `docker compose up -d --build`. Si se para con una canción a medias, al
arrancar la retoma si sigue sonando por donde corresponde; si no, se descarta y la recupera
`recently-played` (si sonó más de 30 s), así que nunca cuenta dos veces.

## 6. Límite diario por canción (F2)

Cuando empieza a sonar una canción que ya tiene `max_plays` escuchas hoy (el día empieza a las
`day_starts_at`), al llegar a `skip_after_s` segundos la salta (si el repeat está en modo
canción, antes lo quita). Cuenta como escucha lo que llega al 50 % o a 4 min y lo que solo
registra recently-played.

Viene en **modo simulación** (`dry_run: true`): solo registra "habría saltado" y avisa por
correo la primera vez de cada canción y día. Para activarlo de verdad, pon `dry_run: false` en
`config.yaml` y `docker compose restart`.

Cada reproducción que supera el límite queda en la tabla `daily_limit_events` con su resultado:
`would_skip` (simulación), `skipped`, `failed` o `cancelled` (la cortaste tú antes de que
actuara).

```sh
docker compose exec regulator python -m sqlite3 /data/regulator.db \
  "SELECT day, track_name, plays_today, max_plays, result FROM daily_limit_events ORDER BY id DESC LIMIT 20"
```

## 7. Estadísticas (F4)

`docker compose up -d` arranca también el contenedor `web`. Desde cualquier dispositivo de tu
red, abre `http://<IP del homelab>:8080`: escuchas, canciones y artistas más escuchados,
repetición, tasa de skips, diversidad de las últimas 8 semanas (artistas distintos, nuevos y
entropía) y las superaciones del límite diario. Se puede ver por día, semana o mes, y descargar
las escuchas y el límite diario en CSV.

La web solo lee la base de datos y no tiene login: **no abras el puerto 8080 en el router**.

## Desarrollo local

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

También puedes hacer login y `check` en local (`.venv/bin/regulator login`), sin túnel: el
navegador y el servidor del callback están en la misma máquina.
