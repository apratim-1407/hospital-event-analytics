# One image, two roles: the API and the Celery worker differ only in entrypoint,
# so what the API validates is exactly what the worker processes.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/benchmarks \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Tooling image: pytest, Locust, generator, benchmark. Not used for serving.
FROM runtime AS dev
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
USER app
