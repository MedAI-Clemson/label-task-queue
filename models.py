from typing import Optional, List
import enum
from fastapi import UploadFile, File

from pydantic import EmailStr, FileUrl
from sqlmodel import Field, Relationship, Enum, Column, String, SQLModel


# Link models
class ProjectTasksetLink(SQLModel, table=True):
    project_id: Optional[int] = Field(
        default=None, foreign_key="project.id", primary_key=True
    )
    taskset_id: Optional[int] = Field(
        default=None, foreign_key="taskset.id", primary_key=True
    )


class ProjectUserLink(SQLModel, table=True):
    project_id: Optional[int] = Field(
        default=None, foreign_key="project.id", primary_key=True
    )
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )


# Taskset models
class TasksetBase(SQLModel):
    name: str
    description: Optional[str]


class Taskset(TasksetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    data_file: Optional[FileUrl]
    projects: List["Project"] = Relationship(
        back_populates="tasksets", link_model=ProjectTasksetLink
    )


class TasksetCreate(TasksetBase):
    pass


class TasksetRead(TasksetBase):
    id: int


class TasksetUpdate(TasksetBase):
    name: Optional[str]


# User models
class Role(enum.Enum):
    labeler = "Labeler"
    admin = "Admin"


class UserBase(SQLModel):
    name: str
    email: EmailStr = Field(sa_column=Column("email", String, unique=True))
    role: Role = Field(sa_column=Column(Enum(Role)))


class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    projects: List["Project"] = Relationship(
        back_populates="users", link_model=ProjectUserLink
    )


class UserCreate(UserBase):
    pass


class UserRead(UserBase):
    id: int


class UserUpdate(UserBase):
    name: Optional[str]
    email: Optional[EmailStr]
    role: Optional[Role]


# Project models
class ProjectBase(SQLModel):
    name: str
    description: Optional[str]


class Project(ProjectBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tasksets: List["Taskset"] = Relationship(
        back_populates="projects", link_model=ProjectTasksetLink
    )
    users: List["User"] = Relationship(
        back_populates="projects", link_model=ProjectUserLink
    )


class ProjectCreate(ProjectBase):
    pass


class ProjectRead(ProjectBase):
    id: int


class ProjectUpdate(ProjectBase):
    name: Optional[str]


class ProjectReadWithTasksetsAndUsers(ProjectRead):
    tasksets: List[TasksetRead]
    users: List[UserRead]


class TasksetReadWithProjects(TasksetRead):
    projects: List[ProjectRead]


class UserReadWithProjects(UserRead):
    projects: List[ProjectRead]
