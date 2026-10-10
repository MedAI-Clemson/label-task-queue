import os
from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

DATABASE_URI = os.environ.get("DATABASE_URI", "sqlite:///./database.db")
DATABASE_ECHO = os.environ.get("DATABASE_ECHO", "").lower() in {"1", "true", "yes"}

connect_args = {"check_same_thread": False} if DATABASE_URI.startswith("sqlite") else {}
engine = create_engine(DATABASE_URI, echo=DATABASE_ECHO, connect_args=connect_args)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
