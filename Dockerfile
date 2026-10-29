FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HLGUARD_DATA_DIR=/app/data \
    HLGUARD_VENUE=paper \
    HLGUARD_MARKET_SOURCE=file \
    HLGUARD_ROOT=/app

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin holdfast

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY evals ./evals
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir . \
    && chmod 0755 /entrypoint.sh \
    && mkdir -p /app/data \
    && chown -R holdfast:holdfast /app

EXPOSE 8741
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "hlguard", "serve", "--host", "0.0.0.0", "--port", "8741", "--demo"]
