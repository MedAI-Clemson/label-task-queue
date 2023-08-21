from typing import Optional, List, Dict, Annotated, Union
import enum
from datetime import datetime
import random

from pydantic import BaseModel, EmailStr
from sqlmodel import (
    Field,
    Relationship,
    Enum,
    Column,
    String,
    SQLModel,
    JSON,
    Integer,
    DateTime,
)


#
# Record models - records represent individual data items
#
class RecordBase(SQLModel):
    data: Dict = Field(default={}, sa_column=Column(JSON))


class Record(RecordBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    # records are required to belong to a dataset
    dataset_id: int = Field(default=None, foreign_key="dataset.id", index=True)

    dataset: "Dataset" = Relationship(back_populates="records")
    tasks: "Task" = Relationship(back_populates="record")


class RecordRead(RecordBase):
    id: int


class RecordCreate(RecordBase):
    pass


class RecordUpdate(RecordBase):
    data: Optional[Dict]


#
# Dataset models
# we add a dataset model to collect a set of records
#
class DatasetBase(SQLModel):
    name: str
    description: Optional[str]


class Dataset(DatasetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)

    records: List["Record"] = Relationship(back_populates="dataset")
    labelqueues: List["LabelQueue"] = Relationship(back_populates="dataset")
    tasks: List["Task"] = Relationship(back_populates="dataset")


class DatasetRead(DatasetBase):
    id: int


class RecordReadWithDataset(RecordRead):
    dataset: DatasetRead


class DatasetCreate(DatasetBase):
    pass


class DatasetUpdate(DatasetBase):
    name: Optional[str]


#
# Link models for LabelQueue, dataset, and user many-to-many relationships
#
class LabelQueueUserLink(SQLModel, table=True):
    labelqueue_id: Optional[int] = Field(
        default=None, foreign_key="labelqueue.id", primary_key=True
    )
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )


#
# Tasks
#


class TaskBase(SQLModel):
    """
    Task models
    - an item from a dataset that is assigned to a particular user
    - includes the completed data payload once completed
    - tasks are only created in the context of a labelqueue
    - the labelqueue must have at least one user, incomplete queuestep, and non-empty dataset
    """

    pass


class Task(TaskBase, table=True):
    # id variables
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    record_id: int = Field(default=None, foreign_key="record.id")
    dataset_id: int = Field(default=None, foreign_key="dataset.id")
    user_id: int = Field(default=None, foreign_key="user.id", index=True)
    queuestep_id: int = Field(default=None, foreign_key="queuestep.id")
    labelqueue_id: int = Field(default=None, foreign_key="labelqueue.id", index=True)

    # data
    created_at: datetime = Field(
        sa_column=Column(DateTime, nullable=False, default=datetime.utcnow),
        index=True,
    )
    completed: bool = Field(default=False, index=True)
    completed_data: Dict = Field(default={}, sa_column=Column(JSON))

    # relationships
    record: "Record" = Relationship(back_populates="tasks")
    dataset: "Dataset" = Relationship(back_populates="tasks")
    user: "User" = Relationship(back_populates="tasks")
    queuestep: "QueueStep" = Relationship(back_populates="tasks")
    labelqueue: "LabelQueue" = Relationship(back_populates="tasks")


class TaskRead(TaskBase):
    id: int
    created_at: datetime
    completed: bool
    completed_data: Dict


class TaskUpdate(TaskBase):
    completed_data: Optional[Dict]


class NextTask(BaseModel):
    """
    NextTask represents the metadata produced by the queue to specify a task to pass to the labeler.
    """

    dataset_id: int
    record_id: int
    queuestep_id: int


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
    labelqueues: List["LabelQueue"] = Relationship(
        back_populates="users", link_model=LabelQueueUserLink
    )

    tasks: List["Task"] = Relationship(back_populates="user")


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
    sequential = "sequential"
    random = "random"
    consensus = "consensus"
    priority = "priority"


class QueueStepBase(SQLModel):
    name: str
    description: Optional[str]
    num_records: Annotated[int, Field(gt=0)]
    type: QueueType = Field(sa_column=Column(Enum(QueueType)))
    policy_args: Dict = Field(default={}, sa_column=Column(JSON))


class QueueStep(QueueStepBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    labelqueue_id: int = Field(default=None, foreign_key="labelqueue.id", index=True)
    num_records_completed: int = 0
    rank: int = Field(default=None, sa_column=Column("rank", Integer, unique=True))
    completed: bool = False

    labelqueue: "LabelQueue" = Relationship(back_populates="queuesteps")
    tasks: List["Task"] = Relationship(back_populates="queuestep")

    def get_next_task(self, user_id) -> NextTask:
        match self.type:
            case QueueType.sequential:
                task = self._get_next_task_sequential()
            case QueueType.random:
                task = self._get_next_task_random()
            case QueueType.consensus:
                task = self._get_next_task_consensus(user_id)
            case QueueType.priority:
                task = self._get_next_task_priority(user_id)
            case _:
                raise NotImplementedError(
                    f"The {self.type.name} queue policy has not been implemented."
                )

        return task

    def _get_next_task_sequential(self) -> Union[NextTask, None]:
        remaining_record_ids = self._get_remaining_records()
        if len(remaining_record_ids) == 0:
            return None

        # for the sequential policy, simply select the task with minimum id
        # this does not take advantage of the table index
        record_id = min(remaining_record_ids)

        return NextTask(
            dataset_id=self.labelqueue.dataset_id,
            record_id=record_id,
            queuestep_id=self.id,
        )

    def _get_next_task_random(self) -> Union[NextTask, None]:
        remaining_record_ids = self._get_remaining_records()
        if len(remaining_record_ids) == 0:
            return None

        record_id = random.choice(remaining_record_ids)

        return NextTask(
            dataset_id=self.labelqueue.dataset_id,
            record_id=record_id,
            queuestep_id=self.id,
        )

    def _get_next_task_consensus(self, user_id) -> Union[NextTask, None]:
        raise NotImplementedError("_get_next_task_consensus has not been implemented")

    def _get_next_task_priority(self, user_id) -> Union[NextTask, None]:
        raise NotImplementedError("_get_next_task_priority has not been implemented")

    def _get_remaining_records(self):
        assigned_record_ids = {task.record.id for task in self.labelqueue.tasks}

        return [
            record.id
            for record in self.labelqueue.dataset.records
            if record.id not in assigned_record_ids
        ]


class QueueStepRead(QueueStepBase):
    id: int
    num_records_completed: int
    rank: int
    completed: bool


class QueueStepCreate(QueueStepBase):
    pass


class QueueStepUpdate(QueueStepBase):
    name: Optional[str]
    num_records: Optional[int]
    type: Optional[QueueType]
    policy_args: Optional[Dict]


#
# LabelQueue models
#
class LabelQueueBase(SQLModel):
    name: str
    description: Optional[str]


class LabelQueue(LabelQueueBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", index=True
    )

    # relationships
    dataset: Optional[Dataset] = Relationship(back_populates="labelqueues")
    users: List[User] = Relationship(
        back_populates="labelqueues", link_model=LabelQueueUserLink
    )
    queuesteps: List[QueueStep] = Relationship(back_populates="labelqueue")
    tasks: List[Task] = Relationship(back_populates="labelqueue")

    def get_active_queuestep(self) -> Union[QueueStep, None]:
        """
        The active questep is the queuestep with lowest rank that is not completed.
        """
        incomplete_queuesteps: List[QueueStep] = list(
            filter(lambda x: not x.completed, self.queuesteps)
        )

        if len(incomplete_queuesteps) < 1:
            return None
        else:
            return min(incomplete_queuesteps, key=lambda x: x.rank)

    def get_next_task(self, user_id) -> NextTask:
        """
        Get a qualifying next task for the user using the queuestep policy.
        """

        active_queuestep = self.get_active_queuestep()

        return active_queuestep.get_next_task(user_id)


class LabelQueueCreate(LabelQueueBase):
    pass


class LabelQueueRead(LabelQueueBase):
    id: int


class LabelQueueUpdate(LabelQueueBase):
    name: Optional[str]


class LabelQueueReadWithRelations(LabelQueueRead):
    dataset: Optional[Dataset]
    users: List[UserRead]
    queuesteps: List[QueueStepRead]
    tasks: List[TaskRead]


#
# Read classes that rely on other classes defined below them
class UserReadWithLabelQueues(UserRead):
    labelqueues: List[LabelQueueRead]


class QueueStepReadWithLabelQueue(QueueStepRead):
    labelqueue: LabelQueueRead


class TaskReadWithRelations(TaskRead):
    dataset: Dataset
    record: Record
    user: User
    labelqueue: LabelQueue
    queuestep: QueueStep


class DatasetReadWithRelations(DatasetRead):
    records: List["RecordRead"]
    labelqueues: List["LabelQueue"]
