"""Structured JSON logging on stdout (one object per line).

Containers are ephemeral and the platform collects stdout, so there are no log
files. Every record carries service, level, event and, where one exists, a
correlation_id so a single `jq` filter reconstructs a request across the API,
the broker and every worker that touched it.

Never log: passwords, raw API keys, JWTs, or request bodies that contain
patient references.
"""
import logging
import socket
import sys

import structlog

from app.core.config import settings


def _add_service(_, __, event_dict):
    name = settings.service_name
    if name == "worker":
        name = f"worker@{socket.gethostname()}"
    event_dict.setdefault("service", name)
    return event_dict


def configure_logging() -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        _add_service,
    ]
    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Route third-party loggers through the same JSON handler. Request logging is
    # done by our own middleware, so uvicorn's access log is silenced.
    for name in ("uvicorn", "uvicorn.error", "celery", "celery.app.trace", "celery.worker"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
