"""The web API (FastAPI): accounts and login, projects, and the labeling engine over HTTP.

``create_app(settings)`` builds an app; ``app`` is the one uvicorn serves
(``uvicorn labelflow.api:app``), configured from the environment (see ``settings``).
"""

from labelflow.api.app import create_app
from labelflow.api.settings import Settings

app = create_app()

__all__ = ["Settings", "app", "create_app"]
