from django.apps import AppConfig
from django.db.backends.signals import connection_created


class HabitsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "habits"

    def ready(self):
        connection_created.connect(_configure_sqlite_connection, dispatch_uid="habits.sqlite.pragmas")


def _configure_sqlite_connection(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    # This environment intermittently fails rollback-journal disk writes.
    # In-memory journaling keeps local development stable.
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=MEMORY;")
