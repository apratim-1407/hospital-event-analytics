"""Celery application: the scaling unit of the processing tier.

Three settings carry the failure-tolerance story:

* task_acks_late: a task is acknowledged to the broker only after it finishes, not
  when it is received. A worker that dies mid-task never acknowledges, so the broker
  redelivers the task to a surviving worker once the visibility timeout expires.
* worker_prefetch_multiplier=1: a worker holds at most one unacknowledged task per
  process, so a crash strands very little work.
* broker visibility_timeout (settings.celery_visibility_timeout): how long a dead
  worker's tasks stay invisible. Celery's default is one hour.
"""
import structlog
from celery import Celery
from celery.signals import setup_logging, worker_process_init, worker_ready, worker_shutdown

from app.core.config import settings
from app.core.logging import configure_logging

def _check_every_tick() -> None:
    """Make kombu's unacked-message recovery act on every 10 s check instead of every 10th.

    Safe because all pipeline tasks are idempotent: restoring a message that is in fact still
    being processed only causes a harmless duplicate execution."""
    try:
        from kombu.transport import redis as kombu_redis

        original = kombu_redis.QoS.restore_visible

        def restore_visible(self, start=0, num=10, interval=1):
            return original(self, start, num, interval)

        kombu_redis.QoS.restore_visible = restore_visible
    except Exception:  # a different kombu layout: fall back to stock behaviour
        pass


_check_every_tick()

celery_app = Celery(
    "hospital",
    broker=settings.broker_url,
    backend=settings.backend_url,
    include=["app.pipeline.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=2,
    broker_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout,
        "socket_connect_timeout": 2,
        "socket_timeout": 10,
    },
    result_backend_transport_options={"retry_policy": {"timeout": 5.0}},
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    task_track_started=False,
    worker_send_task_events=False,
)

log = structlog.get_logger("worker")


@setup_logging.connect
def _configure_logging(**_):
    configure_logging()


@worker_process_init.connect
def _reset_db_pool(**_):
    # Forked children must not share connections opened (if any) by the parent.
    from app.core.db import engine

    engine.dispose(close=False)


@worker_ready.connect
def _on_ready(**_):
    log.info("worker.started", concurrency=settings.celery_concurrency, visibility_timeout=settings.celery_visibility_timeout)


@worker_shutdown.connect
def _on_shutdown(**_):
    log.warning("worker.shutdown")
