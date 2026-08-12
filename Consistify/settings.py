from pathlib import Path
import os
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_env_list(raw_value, default_values):
    values = [item.strip() for item in raw_value.split(",") if item.strip()] if raw_value else []
    return values + [value for value in default_values if value not in values]

# Security
SECRET_KEY = os.environ.get('SECRET_KEY', 'fallback-secret-key')

DEBUG = os.environ.get('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = _parse_env_list(
    os.environ.get('ALLOWED_HOSTS', ''),
    [
        'localhost',
        '127.0.0.1',
        'consistify-app.onrender.com',
    ],
)

CRON_SECRET = os.environ.get('CRON_SECRET', '')


# Applications
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'habits',
]


# Middleware
# The structured logging middleware must be the outermost layer so it sees
# every request and every exception, including ones raised by Django's
# own session, auth, or CSRF middleware below. WhiteNoise is inserted
# *inside* the exception logger so its own middleware errors are also
# captured.
MIDDLEWARE = [
    'habits.middleware.StructuredExceptionMiddleware',
    'habits.middleware.RequestContextLogMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

if not DEBUG:
    # Insert WhiteNoise right after SecurityMiddleware so static files are
    # served before any application code runs, but still inside our
    # structured exception middleware.
    MIDDLEWARE.insert(3, 'whitenoise.middleware.WhiteNoiseMiddleware')

ROOT_URLCONF = 'Consistify.urls'


# Templates
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],  # optional
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'habits.context_processors.friend_request_notifications',
                'habits.context_processors.daily_recap_prompt',
            ],
        },
    },
]


WSGI_APPLICATION = 'Consistify.wsgi.application'


# Database (Neon / PostgreSQL via DATABASE_URL)
#
# Connection robustness settings:
#   * ``conn_max_age=600`` keeps individual connections alive between
#     requests so we avoid the SSL reconnect overhead that Neon charges for.
#   * ``connect_timeout`` guards against a dead PG host hanging the worker
#     indefinitely; the Gunicorn timeout will eventually kill it, but a
#     shorter, explicit timeout produces a clean 500 with a useful traceback
#     far sooner.
#   * ``application_name`` shows up in ``pg_stat_activity`` so production
#     incidents can be traced back to this app rather than to "unknown".
#   * ``conn_health_checks=True`` asks Django to validate the connection on
#     each request so a stale Neon connection after an idle window does not
#     surface as a transient 500.
DATABASE_URL = os.environ.get('DATABASE_URL') or f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
# ``dj_database_url.config`` reads ``DATABASE_URL`` from the environment
# directly (ignoring the ``default=`` we pass when the variable is set but
# empty), so reflect the resolved value back into the environment to keep
# both code paths in sync. Without this, an explicitly empty
# ``DATABASE_URL`` (e.g. exported by a shell profile) produces
# ``django.db.backends.dummy`` and every request crashes with
# ``ImproperlyConfigured`` the moment the session middleware hits the DB.
os.environ['DATABASE_URL'] = DATABASE_URL
DATABASES = {
    'default': dj_database_url.config(
        default=DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
    )
}
# Inject the application name on PostgreSQL backends only. SQLite ignores
# extra kwargs, but psycopg2 will reject unknown kwargs so the lookup must
# be guarded. ``connect_timeout`` is a psycopg2 connect() kwarg so it must
# live under OPTIONS (dj_database_url.config does not forward ``options``).
if DATABASE_URL.startswith(("postgres://", "postgresql://")):
    db_options = DATABASES['default'].setdefault('OPTIONS', {})
    db_options.setdefault('connect_timeout', 10)
    db_options.setdefault('application_name', 'consistify')


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Dhaka'
USE_I18N = True
USE_TZ = True


# Static files
#
# In production we use WhiteNoise's ``CompressedManifestStaticFilesStorage``.
# That backend hashes every file referenced by a template and embeds the hash
# in the URL. If a template references a static file that does not exist on
# disk after ``collectstatic`` ran, every page that includes that reference
# raises ``ValueError: Missing staticfiles manifest entry`` and returns a
# 500. To avoid that, the storage backend is configured to log a warning
# and return the unhashed path so the file still loads even when the
# manifest is incomplete (for example when an optional favicon is missing).
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage'
            if DEBUG
            else 'habits.staticfiles_storage.SafeCompressedManifestStaticFilesStorage'
        ),
    },
}


# Auth redirects
LOGIN_REDIRECT_URL = 'habits:today'
LOGOUT_REDIRECT_URL = 'habits:index'
LOGIN_URL = 'habits:login'


# CSRF (important for Render)
CSRF_TRUSTED_ORIGINS = [
    'https://consistify-app.onrender.com',
]


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Custom error handlers. Returning JSON for AJAX callers keeps the front-end
# from breaking when an unexpected exception occurs anywhere in the request
# cycle. The same handlers also cover 404/403/400 so every error response
# uses the same {ok, error, code} envelope.
HANDLER400 = 'habits.error_handlers.bad_request'
HANDLER403 = 'habits.error_handlers.permission_denied'
HANDLER404 = 'habits.error_handlers.page_not_found'
HANDLER500 = 'habits.error_handlers.server_error'


# Logging configuration. In production the console handler uses a JSON
# formatter so log aggregators can ingest the records directly. The
# ``habits`` and ``habits.errors`` loggers are explicit so production
# deployments can tune verbosity without changing application code.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'plain': {
            '()': 'habits.middleware.RequestIdLogFormatter',
            'format': '%(asctime)s %(levelname)s %(name)s [req=%(request_id)s] %(message)s',
        },
        'verbose': {
            '()': 'habits.middleware.RequestIdLogFormatter',
            'format': (
                '%(asctime)s %(levelname)s %(name)s '
                '[req=%(request_id)s] %(message)s'
            ),
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose' if DEBUG else 'verbose',
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'habits': {
            'handlers': ['console'],
            'level': 'INFO' if not DEBUG else 'DEBUG',
            'propagate': False,
        },
        'habits.errors': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}
