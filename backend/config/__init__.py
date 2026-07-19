"""Ensures the Celery app is loaded when Django starts, so `@shared_task` works
across all apps without an explicit import (T0.3)."""

from .celery import app as celery_app  # noqa: F401

__all__ = ("celery_app",)
