import enum
import random
from datetime import datetime, timezone
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, EmailStr, model_validator
from sqlmodel import (
    JSON,
    Column,
    DateTime,
    Enum,
    Field,
    Relationship,
    SQLModel,
    String,
    UniqueConstraint,
)


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (SQLite does not store timezones)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


#
# Record models - records represent individual data items
#
class RecordBase(SQLModel):
    data: dict = Field(default_factory=dict, sa_column=Column(JSON))


class Record(RecordBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    # records are required to belong to a dataset
    dataset_id: int = Field(default=None, foreign_key="dataset.id", index=True)

    dataset: "Dataset" = Relationship(back_populates="records")
    tasks: list["Task"] = Relationship(back_populates="record")


class RecordRead(RecordBase):
    id: int


class RecordCreate(RecordBase):
    pass


class RecordUpdate(RecordBase):
    data: Optional[dict] = None


#
# Dataset models
# we add a dataset model to collect a set of records
#
class DatasetBase(SQLModel):
    name: str
    description: Optional[str] = None


class Dataset(DatasetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True, index=True)

    # records belong to their dataset and are deleted with it
    records: list["Record"] = Relationship(back_populates="dataset", cascade_delete=True)
    labelqueues: list["LabelQueue"] = Relationship(back_populates="dataset")
    tasks: list["Task"] = Relationship(back_populates="dataset")


class DatasetRead(DatasetBase):
    id: int


class RecordReadWithDataset(RecordRead):
    dataset: DatasetRead


class DatasetCreate(DatasetBase):
    pass


class DatasetUpdate(DatasetBase):
    name: Optional[str] = None


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
        default_factory=utcnow,
        sa_column=Column(DateTime, nullable=False, index=True),
    )
    completed: bool = Field(default=False, index=True)
    completed_data: dict = Field(default_factory=dict, sa_column=Column(JSON))

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
    completed_data: dict


class TaskUpdate(TaskBase):
    completed_data: Optional[dict] = None


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
    labelqueues: list["LabelQueue"] = Relationship(
        back_populates="users", link_model=LabelQueueUserLink
    )

    tasks: list["Task"] = Relationship(back_populates="user")


class UserCreate(UserBase):
    pass


class UserRead(UserBase):
    id: int


class UserUpdate(UserBase):
    name: Optional[str] = None
    email: Optional[EmailStr] = None
    role: Optional[Role] = None


#
# QueueStep
#
class QueueType(enum.Enum):
    distribute = "distribute"
    consensus = "consensus"
    priority = "priority"


class PolicyArgsBase(BaseModel):
    model_config = ConfigDict(validate_assignment=True, from_attributes=True, extra="forbid")


class PolicyArgsDistribute(PolicyArgsBase):
    random: bool = False


class PolicyArgsConsensus(PolicyArgsBase):
    pass


PolicyArgs = Union[PolicyArgsDistribute, PolicyArgsConsensus]


def validate_policy_args(queue_type: QueueType, value: Any) -> PolicyArgsBase:
    """
    Validate the provided policy_args data using the model associated with the policy type.
    This is needed because different policy types have different argument structures that need
    separate validation logic.

    If no policy arguments object is passed, instantiate the default.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump()

    match queue_type:
        case QueueType.distribute:
            return PolicyArgsDistribute.model_validate(value or {})
        case _:
            # ValueError (rather than NotImplementedError) so that pydantic reports a
            # validation error (HTTP 422) instead of crashing the request
            raise ValueError(
                f"PolicyArgs has not been implemented for queue type '{queue_type.value}'."
            )


class QueueStepBase(SQLModel):
    name: str
    description: Optional[str] = None
    num_records: int = Field(gt=0)
    type: QueueType = Field(sa_column=Column(Enum(QueueType)))


class QueueStep(QueueStepBase, table=True):
    # rank orders the steps within a single labelqueue
    __table_args__ = (UniqueConstraint("labelqueue_id", "rank"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    labelqueue_id: int = Field(default=None, foreign_key="labelqueue.id", index=True)
    # policy arguments are validated by QueueStepCreate and stored as plain JSON
    policy_args: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    num_records_completed: int = 0
    rank: int = Field(default=None)
    completed: bool = False

    labelqueue: "LabelQueue" = Relationship(back_populates="queuesteps")
    tasks: list["Task"] = Relationship(back_populates="queuestep")

    def get_next_task(self, user_id: int) -> Optional[NextTask]:
        match self.type:
            case QueueType.distribute:
                task = self._get_next_task_distribute()
            case QueueType.consensus:
                task = self._get_next_task_consensus(user_id)
            case QueueType.priority:
                task = self._get_next_task_priority(user_id)
            case _:
                raise NotImplementedError(
                    f"The {self.type.name} queue policy has not been implemented."
                )

        return task

    def _get_next_task_distribute(self) -> Optional[NextTask]:
        policy_args = PolicyArgsDistribute.model_validate(self.policy_args or {})

        # the distribute policy assigns each record once, so the step is at capacity
        # once it has handed out num_records tasks
        if len(self.tasks) >= self.num_records:
            return None

        remaining_record_ids = self._get_remaining_records()
        if len(remaining_record_ids) == 0:
            return None

        # for the sequential policy, simply select the task with minimum id
        # this does not take advantage of the table index
        if policy_args.random:
            record_id = random.choice(remaining_record_ids)
        else:
            record_id = min(remaining_record_ids)

        return NextTask(
            dataset_id=self.labelqueue.dataset_id,
            record_id=record_id,
            queuestep_id=self.id,
        )

    def _get_next_task_consensus(self, user_id: int) -> Optional[NextTask]:
        raise NotImplementedError("_get_next_task_consensus has not been implemented")

    def _get_next_task_priority(self, user_id: int) -> Optional[NextTask]:
        raise NotImplementedError("_get_next_task_priority has not been implemented")

    def _get_remaining_records(self) -> list[int]:
        # NOTE: records are considered assigned if they have a task under *any* step of
        # the labelqueue, not only this step. Whether later steps should be able to
        # re-assign records is an open design question.
        assigned_record_ids = {task.record_id for task in self.labelqueue.tasks}

        return [
            record.id
            for record in self.labelqueue.dataset.records
            if record.id not in assigned_record_ids
        ]


class QueueStepRead(QueueStepBase):
    id: int
    policy_args: Optional[dict[str, Any]] = None
    num_records_completed: int
    rank: int
    completed: bool


class QueueStepCreate(QueueStepBase):
    policy_args: Optional[PolicyArgs] = None

    @model_validator(mode="after")
    def check_policy_args_by_type(self) -> "QueueStepCreate":
        self.policy_args = validate_policy_args(self.type, self.policy_args)
        return self


class QueueStepUpdate(QueueStepBase):
    name: Optional[str] = None
    num_records: Optional[int] = Field(default=None, gt=0)
    type: Optional[QueueType] = None
    policy_args: Optional[PolicyArgs] = None


#
# LabelQueue models
#
class LabelQueueBase(SQLModel):
    name: str
    description: Optional[str] = None


class LabelQueue(LabelQueueBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", index=True
    )

    # relationships
    dataset: Optional[Dataset] = Relationship(back_populates="labelqueues")
    users: list[User] = Relationship(
        back_populates="labelqueues", link_model=LabelQueueUserLink
    )
    # queuesteps belong to their labelqueue and are deleted with it
    queuesteps: list[QueueStep] = Relationship(
        back_populates="labelqueue", cascade_delete=True
    )
    tasks: list[Task] = Relationship(back_populates="labelqueue")

    def get_active_queuestep(self) -> Optional[QueueStep]:
        """
        The active questep is the queuestep with lowest rank that is not completed.
        """
        incomplete_queuesteps = [qs for qs in self.queuesteps if not qs.completed]

        if len(incomplete_queuesteps) < 1:
            return None
        else:
            return min(incomplete_queuesteps, key=lambda x: x.rank)

    def get_next_task(self, user_id: int) -> Optional[NextTask]:
        """
        Get a qualifying next task for the user using the queuestep policy.
        Returns None if there is no active queuestep or the active queuestep has no
        task to hand out.
        """

        active_queuestep = self.get_active_queuestep()
        if active_queuestep is None:
            return None

        return active_queuestep.get_next_task(user_id)


class LabelQueueCreate(LabelQueueBase):
    pass


class LabelQueueRead(LabelQueueBase):
    id: int


class LabelQueueUpdate(LabelQueueBase):
    name: Optional[str] = None


class LabelQueueReadWithRelations(LabelQueueRead):
    dataset: Optional[Dataset] = None
    users: list[UserRead]
    queuesteps: list[QueueStepRead]
    tasks: list[TaskRead]


#
# Read classes for model families that rely on other classes defined below them
class UserReadWithLabelQueues(UserRead):
    labelqueues: list[LabelQueueRead]


class QueueStepReadWithLabelQueue(QueueStepRead):
    labelqueue: LabelQueueRead


class TaskReadWithRelations(TaskRead):
    dataset: Dataset
    record: Record
    user: User
    labelqueue: LabelQueue
    queuestep: QueueStep


class DatasetReadWithRelations(DatasetRead):
    records: list["RecordRead"]
    labelqueues: list["LabelQueue"]
