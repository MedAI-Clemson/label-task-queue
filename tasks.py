from pydantic import AnyUrl
from sqlmodel import SQLModel, Field
from typing import Optional


class BaseTask(SQLModel):
    external_id: Optional[str]


class TextTask(BaseTask, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    text: str


class URITask(BaseTask, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    uri: AnyUrl
