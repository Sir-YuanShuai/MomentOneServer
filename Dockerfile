FROM python:3.14-slim AS runtime

ARG BUILD_VERSION=0.1.0
ARG BUILD_COMMIT=unknown
ARG BUILD_TIME

ENV MOMENT_ONE_BUILD_VERSION=${BUILD_VERSION} \
    MOMENT_ONE_BUILD_COMMIT=${BUILD_COMMIT} \
    MOMENT_ONE_BUILD_TIME=${BUILD_TIME} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY pyproject.toml README.md ./
COPY app ./app
COPY contracts ./contracts
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY mcp_apps ./mcp_apps
RUN python -m pip install --upgrade pip && python -m pip install .

USER appuser
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
