from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.notifications"
    label = "notifications"

    def ready(self):
        # T5.2: connects the `@receiver`s wiring M2/M3 domain events to
        # `enqueue_transactional_email` -- same "import the signals module
        # in ready()" pattern as `apps.rbac.apps.RbacConfig`.
        from . import receivers  # noqa: F401
