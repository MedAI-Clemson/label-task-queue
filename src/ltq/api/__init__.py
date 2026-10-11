"""The web API (FastAPI): accounts and login, projects, and the labeling engine over HTTP.

``create_app(settings)`` builds an app; ``app`` is the one uvicorn serves
(``uvicorn ltq.api:app``), configured from the environment (see ``settings``).
"""

from ltq.api.app import create_app
from ltq.api.settings import Settings

app = create_app()

__all__ = ["Settings", "app", "create_app"]
