from typing import Optional, List, Dict
import enum

from pydantic import EmailStr
from sqlmodel import Field, Relationship, Enum, Column, String, SQLModel, JSON


#
# Item models
#
class ItemBase(SQLModel):
    data: Dict = Field(default={}, sa_column=Column(JSON))


class Item(ItemBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    # items are required to belong to a dataset
    dataset_id: int = Field(default=None, foreign_key="dataset.id", index=True)

    dataset: "Dataset" = Relationship(back_populates="items")


class ItemRead(ItemBase):
    id: int


class ItemCreate(ItemBase):
    pass


class ItemUpdate(ItemBase):
    data: Optional[Dict]


#
# Dataset models
# we add a dataset model to collect a set of items
# this allows items to be used across different tasksets
#
class DatasetBase(SQLModel):
    name: str
    description: Optional[str]


class Dataset(DatasetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)

    items: List["Item"] = Relationship(back_populates="dataset")
    tasksets: List["Taskset"] = Relationship(back_populates="dataset")


class DatasetRead(DatasetBase):
    id: int


class ItemReadWithDataset(ItemRead):
    dataset: DatasetRead


class DatasetCreate(DatasetBase):
    pass


class DatasetUpdate(DatasetBase):
    name: Optional[str]


#
# Link models for Project, taskset, and user many-to-many relationships
#
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


#
# Taskset models
#
class TasksetBase(SQLModel):
    name: str
    description: Optional[str]
    # tasksets are required to have a dataset
    dataset_id: int = Field(default=None, foreign_key="dataset.id", index=True)


class Taskset(TasksetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    projects: List["Project"] = Relationship(
        back_populates="tasksets", link_model=ProjectTasksetLink
    )
    dataset: Dataset = Relationship(back_populates="tasksets")


class TasksetCreate(TasksetBase):
    pass


class TasksetRead(TasksetBase):
    id: int


class DatasetReadWithRelations(DatasetRead):
    items: List["ItemRead"]
    tasksets: List["TasksetRead"]


class TasksetUpdate(TasksetBase):
    name: Optional[str]
    dataset_id: Optional[int]


#
# User models
#
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


#
# Project models
#
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


class ProjectReadWithRelations(ProjectRead):
    tasksets: List[TasksetRead]
    users: List[UserRead]


class TasksetReadWithRelations(TasksetRead):
    projects: List["ProjectRead"]
    dataset: DatasetRead


class UserReadWithProjects(UserRead):
    projects: List[ProjectRead]
