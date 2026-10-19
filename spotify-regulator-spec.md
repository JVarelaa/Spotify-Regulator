# Proyecto: Spotify Listening Regulator

## Contexto para Claude Code

Quiero un servicio personal que corra 24/7 en mi homelab (Debian 12, workloads en Docker) y regule cómo escucho música en Spotify. Es para uso personal de una sola cuenta (la mía), no comercial. El objetivo es romper el hábito de escuchar la misma canción en bucle y de saltar canciones compulsivamente, y fomentar descubrir artistas, álbumes y géneros nuevos.

**Antes de escribir código:** verifica en la documentación actual de la Spotify Web API (developer.spotify.com) que cada endpoint que vayas a usar sigue disponible en *Development Mode*. Spotify eliminó y restringió endpoints en nov-2024 y feb/mar-2026, y muchos tutoriales y librerías siguen referenciando endpoints que ya devuelven 403. Si algo de este documento depende de un endpoint no disponible, avísame y propone alternativa en lugar de improvisar.

## Requisitos funcionales

### F1 — Registro de escuchas (MVP, hacer primero)

Dos fuentes con responsabilidades separadas:

**F1a — Historial fiable (fuente de verdad para estadísticas de escuchas):**
- Consultar `GET /me/player/recently-played` cada 30–60 min usando el cursor `after` (timestamp de la última reproducción guardada) para traer solo lo nuevo. Persistir el cursor.
- Deduplicar por (`track_id`, `played_at`).
- Limitación conocida: solo incluye reproducciones de >30 s y como máximo las últimas 50, por eso no sirve para skips ni para tiempo real.

**F1b — Estado en tiempo real (para reglas y detección de skips):**
- Hacer polling de `GET /me/player/currently-playing` (o `GET /me/player`) para saber qué suena, en qué dispositivo, progreso, modo repeat/shuffle.
- Polling adaptativo: intervalo corto (~3–5 s) mientras hay reproducción activa, largo (~30–60 s) cuando no suena nada. Respetar 429 y la cabecera `Retry-After` con backoff exponencial. La cuota de Development Mode se cuenta por cuenta de desarrollador, así que hay que ser conservador.
- Reconstruir "eventos de escucha" a partir de las muestras:
  - **Escucha completada:** la pista llegó a ≥ X% de su duración o ≥ N segundos (configurable; por defecto 50% o 4 min, lo que ocurra antes).
  - **Skip:** la pista cambió antes de ese umbral.
  - **Repetición:** misma pista vuelve a empezar (el progreso se reinicia con el mismo `track_id`, típico de repeat-one).
- Persistir en SQLite (un único fichero en un volumen Docker): pista, artista(s), álbum, timestamp inicio/fin, ms escuchados, tipo de evento, dispositivo, contexto (playlist/álbum) si existe.
- Estos eventos complementan a F1a: los skips solo existen aquí; las escuchas completadas se reconcilian con F1a, que prevalece si hay discrepancia.
- En las estadísticas, poder comparar ambas fuentes para medir cuánto se le escapa al polling.

### F2 — Límite diario de escuchas por canción
- Límite global configurable (p. ej. 3 escuchas/día por canción) y overrides por canción.
- El "día" se resetea a una hora configurable en zona horaria `Atlantic/Canary` (por defecto 04:00, para que una sesión nocturna no se parta en dos días).
- Cuando una canción que ya alcanzó el límite empieza a sonar: `POST /me/player/next` tras un margen corto (p. ej. 5 s) y, si el modo repeat está en `track`, ponerlo en `off` antes.
- Notificación al saltar (ver F5) indicando "límite alcanzado: canción X, 3/3 hoy".

### F3 — Control de skips compulsivos
- Detectar ráfagas: más de N skips en M minutos (configurable, p. ej. 5 skips en 3 min).
- Acción configurable por niveles:
  1. `log`: solo registrar.
  2. `warn`: notificación.
  3. `cooldown`: pausar (`PUT /me/player/pause`) durante K segundos para cortar la inercia.
- Por defecto `warn`. No intentar "bloquear" el botón de skip: la API no lo permite.

### F4 — Estadísticas
- Endpoint web sencillo (FastAPI + HTML server-side o una página estática con gráficos) accesible solo en la LAN:
  - Top canciones/artistas por día/semana/mes.
  - Ratio de repetición (escuchas de la canción más repetida / total).
  - Tasa de skips y ráfagas detectadas.
  - **Métricas de diversidad:** artistas únicos por semana, artistas nuevos (nunca escuchados antes) por semana, entropía de Shannon de la distribución de artistas.
  - Cuántas veces actuó cada regla (F2, F3).
- Exportación CSV de la tabla de eventos.

### F5 — Notificaciones
- Vía ntfy (self-hosted o ntfy.sh) con un topic configurable. Abstraer detrás de una interfaz para poder cambiar a otro canal.

## Requisitos no funcionales

- **Lenguaje:** Python 3.12, `httpx` para la API (o `spotipy` si sigue mantenido y compatible con los cambios de 2026; si no, cliente propio fino).
- **Arquitectura:** un proceso con un bucle asíncrono de polling + motor de reglas, y la API/web en el mismo contenedor o en otro que lea la misma SQLite.
- **Configuración:** fichero `config.yaml` (límites, umbrales, horas, modos) + `.env` para secretos. Recargar config sin reiniciar si es sencillo.
- **OAuth:** Authorization Code con PKCE o Authorization Code normal. Scopes mínimos: `user-read-playback-state`, `user-modify-playback-state`, `user-read-currently-playing`, `user-read-recently-played`. El redirect URI debe usar loopback explícito (`http://127.0.0.1:PUERTO/callback`), no `localhost`. Como el servidor es headless, documenta cómo hacer el login inicial con un túnel SSH (`ssh -L`) desde mi portátil. Guardar el refresh token en el volumen con permisos 600 y manejar la rotación del refresh token.
- **Despliegue:** `Dockerfile` + `docker-compose.yml`, `restart: unless-stopped`, healthcheck, logs a stdout en formato estructurado.
- **Robustez:** no petar si no hay dispositivo activo (204), si suena un podcast/episodio, si hay anuncios, o si se cae la red. Ningún comando de reproducción en paralelo: la API no garantiza el orden entre llamadas del reproductor, así que serializarlas.
- **Modo "dry-run" activado por defecto:** las reglas F2 y F3 se evalúan, se registran en la base de datos ("habría saltado X", "habría pausado") y se notifican, pero no envían comandos de reproducción. Cada regla tiene su propio flag para pasarla a modo real de forma independiente. Las estadísticas deben mostrar cuántas veces habría actuado cada regla, para calibrar umbrales antes de activarlas.
- **Tests:** tests unitarios del reconstructor de eventos (F1) y del motor de reglas con secuencias de muestras simuladas. Es la parte con más lógica y más fácil de romper.

## Orden de trabajo

1. Esqueleto del proyecto, OAuth y comprobación de qué endpoints responden con mi cuenta.
2. F1 completo con tests + docker-compose. Dejarlo corriendo unos días antes de seguir.
3. F2 con dry-run activado.
4. F3.
5. F4 y F5.

Después de cada fase, dime qué has hecho, cómo probarlo y qué decisiones has tomado. Antes de ejecutar comandos que no sean obvios, explícame qué hacen.
