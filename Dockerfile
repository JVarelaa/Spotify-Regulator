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

# Fase 2 añadirá un HEALTHCHECK basado en el latido del bucle de polling.
ENTRYPOINT ["regulator"]
CMD ["run"]
