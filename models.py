from typing import Optional, List, Dict, Annotated
import enum

from pydantic import EmailStr
from sqlmodel import Field, Relationship, Enum, Column, String, SQLModel, JSON, Integer


#
# Record models
#
class RecordBase(SQLModel):
    data: Dict = Field(default={}, sa_column=Column(JSON))


class Record(RecordBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    # records are required to belong to a dataset
    dataset_id: int = Field(default=None, foreign_key="dataset.id", index=True)

    dataset: "Dataset" = Relationship(back_populates="records")


class RecordRead(RecordBase):
    id: int


class RecordCreate(RecordBase):
    pass


class RecordUpdate(RecordBase):
    data: Optional[Dict]


#
# Dataset models
# we add a dataset model to collect a set of records
# this allows records to be used across different tasksets
#
class DatasetBase(SQLModel):
    name: str
    description: Optional[str]


class Dataset(DatasetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)

    records: List["Record"] = Relationship(back_populates="dataset")
    tasksets: List["Taskset"] = Relationship(back_populates="dataset")


class DatasetRead(DatasetBase):
    id: int


class RecordReadWithDataset(RecordRead):
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
    records: List["RecordRead"]
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
# QueueStep
#
class QueueType(enum.Enum):
    random = "Random"
    consensus = "Consensus"
    priority = "Priority"


class QueueStepBase(SQLModel):
    name: str
    description: Optional[str]
    num_records: Annotated[int, Field(gt=0)]
    type: QueueType = Field(sa_column=Column(Enum(QueueType)))
    policy_args: Dict = Field(default={}, sa_column=Column(JSON))


class QueueStep(QueueStepBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(default=None, foreign_key="project.id", index=True)
    num_records_completed: int = 0
    rank: int = Field(default=None, sa_column=Column("rank", Integer, unique=True))

    project: "Project" = Relationship(back_populates="queue_steps")


class QueueStepRead(QueueStepBase):
    id: int
    num_records_completed: int
    rank: int


class QueueStepCreate(QueueStepBase):
    pass


class QueueStepUpdate(QueueStepBase):
    name: Optional[str]
    num_records: Optional[int]
    type: Optional[QueueType]
    policy_args: Optional[Dict]


#
# Project models
#
class ProjectBase(SQLModel):
    name: str
    description: Optional[str]


class Project(ProjectBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tasksets: List[Taskset] = Relationship(
        back_populates="projects", link_model=ProjectTasksetLink
    )
    users: List[User] = Relationship(
        back_populates="projects", link_model=ProjectUserLink
    )
    queue_steps: List[QueueStep] = Relationship(back_populates="project")


class ProjectCreate(ProjectBase):
    pass


class ProjectRead(ProjectBase):
    id: int


class ProjectUpdate(ProjectBase):
    name: Optional[str]


class ProjectReadWithRelations(ProjectRead):
    tasksets: List[TasksetRead]
    users: List[UserRead]
    queue_steps: List[QueueStepRead]


#
# Read classes that rely on other classes defined below them
#
class TasksetReadWithRelations(TasksetRead):
    projects: List[ProjectRead]
    dataset: DatasetRead


class UserReadWithProjects(UserRead):
    projects: List[ProjectRead]


class QueueStepReadWithProject(QueueStepRead):
    project: ProjectRead
