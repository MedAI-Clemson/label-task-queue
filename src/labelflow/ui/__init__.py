"""The server-rendered interface (Jinja2 + HTMX + the Canvas 2D mask tool).

``mount_ui(app)`` mounts it under ``/ui`` of the API app; see ``labelflow.ui.app``.
"""

from labelflow.ui.app import build_ui, mount_ui

__all__ = ["build_ui", "mount_ui"]
