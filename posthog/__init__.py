# This will make sure the app is always imported when
# Django starts so that shared_task will use this app.
from posthog.celery import app as celery_app

__all__ = ("celery_app",)

# snowflake-connector-python tries to access a root folder which errors out in pods.
# This sets the snowflake home directory to a relative folder
import os

os.environ["SNOWFLAKE_HOME"] = "./.snowflake"
