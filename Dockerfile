FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REGULATOR_DATA_DIR=/data \
    REGULATOR_CONFIG=/config/config.yaml \
    REGULATOR_CALLBACK_BIND=0.0.0.0

RUN useradd --create-home --uid 1000 regulator \
    && mkdir -p /data /config \
    && chown regulator:regulator /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER regulator
VOLUME /data

# El bucle del reproductor deja un latido en /data/heartbeat en cada vuelta; si lleva más de
# 2 min sin cambiar, el contenedor se marca como unhealthy (Docker no lo reinicia por eso).
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=2 CMD ["regulator", "healthcheck"]
ENTRYPOINT ["regulator"]
CMD ["run"]
