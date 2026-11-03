"""F4: the stats web page and CSV exports. Read-only, meant for the home network only.

It runs as its own container next to the service (`regulator web`), so a problem here never
stops the recording of listens.
"""

from __future__ import annotations

import logging
import time
from contextlib import closing
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from .config import Config
from .rules import regulator_day
from .stats import MONTHS, Period, PeriodKind, Ranked, Stats, Summary, WeekDiversity

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
LIMIT_RESULTS = (
    ("would_skip", "Habría saltado (simulación)"),
    ("skipped", "Saltada"),
    ("failed", "Falló el salto"),
    ("cancelled", "La cortaste tú antes"),
)


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Spotify Regulator", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=TEMPLATES)
    starts_at = config.daily_limit.day_starts_at

    def open_stats() -> Stats | None:
        if not config.db_path.exists():
            return None
        return Stats(config.db_path, config.timezone, starts_at)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, p: PeriodKind = "week", d: date | None = None) -> HTMLResponse:
        today, _ = regulator_day(int(time.time() * 1000), config.timezone_info, starts_at)
        period = Period.containing(p, min(d or today, today))
        context = {
            "period": period,
            "label": period.label(),
            "kinds": (("day", "Día"), ("week", "Semana"), ("month", "Mes")),
            "previous": period.previous(),
            "next": period.next() if period.end_day <= today else None,
            "is_current": period.first_day <= today < period.end_day,
            "day_starts_at": starts_at.strftime("%H:%M"),
            "timezone": config.timezone,
        }
        stats = open_stats()
        if stats is None:
            return templates.TemplateResponse(request, "index.html", {**context, "empty_db": True})
        with closing(stats):
            summary = stats.summary(period)
            weeks = stats.diversity(Period.containing("week", min(period.last_day, today)))
            first_ms = stats.first_listen_ms()
        context.update(
            summary=summary,
            view=_summary_view(summary),
            diversity=_diversity_view(weeks),
            data_since=_date_es(stats_day(first_ms, config)) if first_ms else None,
        )
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            stats = open_stats()
            if stats is None:
                raise FileNotFoundError(config.db_path)
            with closing(stats):
                stats.first_listen_ms()
        except Exception as exc:  # noqa: BLE001 - any failure means "not healthy"
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": True})

    @app.get("/export/escuchas.csv")
    def export_events() -> Response:
        return _csv(open_stats, "events_csv", "escuchas.csv")

    @app.get("/export/limite-diario.csv")
    def export_daily_limit() -> Response:
        return _csv(open_stats, "daily_limit_csv", "limite-diario.csv")

    return app


def stats_day(ms: int, config: Config) -> date:
    return regulator_day(ms, config.timezone_info, config.daily_limit.day_starts_at)[0]


def _csv(open_stats, method: str, filename: str) -> Response:
    stats = open_stats()
    if stats is None:
        return Response("Todavía no hay base de datos.", status_code=404)
    with closing(stats):
        text = getattr(stats, method)()
    # The BOM lets Excel and LibreOffice open accented names correctly.
    return Response(
        text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _summary_view(s: Summary) -> dict:
    return {
        "top_songs": _bars(s.top_songs),
        "top_artists": _bars(s.top_artists),
        "repetition": _percent(s.repetition),
        "skip_rate": _percent(s.skip_rate),
        "limit_results": [(label, s.limit_results.get(key, 0)) for key, label in LIMIT_RESULTS],
        "limit_top": _bars(s.limit_top),
    }


def _bars(items: list[Ranked]) -> list[dict]:
    top = max((i.count for i in items), default=0)
    return [{"name": i.name, "detail": i.detail, "count": i.count, "pct": 100 * i.count / top if top else 0}
            for i in items]


def _diversity_view(weeks: list[WeekDiversity]) -> dict:
    def chart(title: str, values: list[float], fmt) -> dict:
        top = max(values, default=0)
        return {
            "title": title,
            "columns": [
                {"label": _short(w.first_day), "week": _date_es(w.first_day), "value": fmt(v),
                 "pct": 100 * v / top if top else 0, "current": i == len(weeks) - 1}
                for i, (w, v) in enumerate(zip(weeks, values, strict=True))
            ],
        }

    return {
        "charts": [
            chart("Artistas distintos", [w.artists for w in weeks], str),
            chart("Artistas nuevos", [w.new_artists for w in weeks], str),
            chart("Entropía (bits)", [w.entropy_bits for w in weeks], _decimal),
        ],
        "rows": [
            {"week": _date_es(w.first_day), "listens": w.listens, "artists": w.artists,
             "new_artists": w.new_artists, "entropy": _decimal(w.entropy_bits)}
            for w in weeks
        ],
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{round(value * 100)} %"


def _decimal(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _short(day: date) -> str:
    return f"{day.day}/{day.month}"


def _date_es(day: date) -> str:
    return f"{day.day} de {MONTHS[day.month - 1]} de {day.year}"
