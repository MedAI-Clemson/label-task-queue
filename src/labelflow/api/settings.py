"""Deployment settings, read from environment variables.

=====================  =========================  ==========================================
Variable               Default                    Meaning
=====================  =========================  ==========================================
``DATABASE_URI``       ``sqlite:///./db/labelflow.db``  The SQLite database.
``LABELFLOW_LABEL_TASKS``    ``./ontologies``           Folder of ontology files (label tasks).
``LABELFLOW_MEDIA_ROOT``     (not set)                  Folder the server may read images from,
                                                  to hash them at import.
``LABELFLOW_SESSION_HOURS``  ``12``                     How long a login lasts.
``LABELFLOW_COOKIE_SECURE``  ``false``                  Mark the login cookie "https only".
=====================  =========================  ==========================================

``LABELFLOW_SQLITE_SYNCHRONOUS`` (default ``FULL``) is SQLite's durability setting; leave it alone
except for experiments.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_uri: str = "sqlite:///./db/labelflow.db"
    label_tasks: Path = Path("./ontologies")
    media_root: Path | None = None
    session_hours: float = 12.0
    cookie_secure: bool = False
    sqlite_synchronous: str = "FULL"
    password_cost: int = 14
    """log2 of scrypt's CPU/memory cost. Only tests lower it; it is not an environment setting."""

    def __post_init__(self) -> None:
        if not math.isfinite(self.session_hours) or self.session_hours <= 0:
            raise ValueError("The session lifetime must be a positive number of hours.")

    @property
    def session_lifetime(self) -> timedelta:
        return timedelta(hours=self.session_hours)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from ``env`` (default: the process environment)."""
        env = os.environ if env is None else env
        defaults = cls()
        media = env.get("LABELFLOW_MEDIA_ROOT", "").strip()
        return cls(
            database_uri=env.get("DATABASE_URI", "").strip() or defaults.database_uri,
            label_tasks=Path(env.get("LABELFLOW_LABEL_TASKS", "").strip() or defaults.label_tasks),
            media_root=Path(media) if media else None,
            session_hours=_number(env, "LABELFLOW_SESSION_HOURS", defaults.session_hours),
            cookie_secure=_flag(env, "LABELFLOW_COOKIE_SECURE", defaults.cookie_secure),
            sqlite_synchronous=(
                env.get("LABELFLOW_SQLITE_SYNCHRONOUS", "").strip() or defaults.sqlite_synchronous
            ),
        )


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        value = 0
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number, not {text!r}.")
    return value


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    text = env.get(name, "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, not {text!r}.")
