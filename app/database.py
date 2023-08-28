import os

from sqlmodel import create_engine, SQLModel, Session

sqlite_url = os.environ["DATABASE_URI"]
connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, echo=True, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(
        engine,
    )


def get_session():
    with Session(engine) as session:
        yield session
