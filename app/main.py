from contextlib import asynccontextmanager
from typing import Annotated, Optional, TypeVar

from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app.database import create_db_and_tables, get_session
from app.models import (
    Dataset,
    DatasetCreate,
    DatasetReadWithRelations,
    DatasetUpdate,
    LabelQueue,
    LabelQueueCreate,
    LabelQueueReadWithRelations,
    LabelQueueUpdate,
    NextTask,
    QueueStep,
    QueueStepCreate,
    QueueStepReadWithLabelQueue,
    QueueStepUpdate,
    Record,
    RecordCreate,
    RecordReadWithDataset,
    RecordUpdate,
    Task,
    TaskReadWithRelations,
    User,
    UserCreate,
    UserReadWithLabelQueues,
    UserUpdate,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(lifespan=lifespan, swagger_ui_parameters={"tryItOutEnabled": True})

SessionDep = Annotated[Session, Depends(get_session)]

ModelT = TypeVar("ModelT", bound=SQLModel)


# TODO: get specific task
# TODO: get user tasks
# TODO: implement consensus policy


#
# Helpers
#
def get_or_404(session: Session, model: type[ModelT], obj_id: int) -> ModelT:
    """Fetch a row by primary key or raise a 404 named after the model."""
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


def apply_update(db_obj: SQLModel, update: SQLModel, create_model: type[SQLModel]) -> None:
    """
    Apply the fields set in a PATCH body to a table object.

    The merged result is validated with the corresponding create model so that a PATCH
    cannot produce a row that could not have been created (e.g. a null name, or
    policy_args that do not match the queue step type).
    """
    fields = set(create_model.model_fields)
    merged = {field: getattr(db_obj, field) for field in fields}
    merged.update(update.model_dump(exclude_unset=True))
    try:
        validated = create_model.model_validate(merged)
    except ValidationError as e:
        # report errors like FastAPI's request validation does (located in the body);
        # the merged input is left out since it is not necessarily JSON serializable
        raise HTTPException(
            status_code=422,
            detail=[
                {"type": err["type"], "loc": ["body", *err["loc"]], "msg": err["msg"]}
                for err in e.errors(include_url=False)
            ],
        )
    db_obj.sqlmodel_update(validated.model_dump(include=fields))


def ensure_no_tasks(obj: Dataset | Record | User | QueueStep | LabelQueue) -> None:
    """Tasks record labeling work, so objects that tasks refer to cannot be deleted."""
    if obj.tasks:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete {type(obj).__name__} because it has tasks.",
        )


#
# Datasets
#
@app.post("/datasets/", response_model=DatasetReadWithRelations, tags=["Dataset"])
def create_dataset(*, session: SessionDep, dataset: DatasetCreate):
    db_dataset = Dataset.model_validate(dataset)
    session.add(db_dataset)
    session.commit()
    session.refresh(db_dataset)
    return db_dataset


@app.get("/datasets/", response_model=list[DatasetReadWithRelations], tags=["Dataset"])
def get_datasets(*, session: SessionDep):
    return session.exec(select(Dataset)).all()


@app.get(
    "/datasets/{dataset_id}", response_model=DatasetReadWithRelations, tags=["Dataset"]
)
def get_dataset(*, session: SessionDep, dataset_id: int):
    return get_or_404(session, Dataset, dataset_id)


@app.post("/datasets/{dataset_id}/records/", tags=["Dataset"])
def create_records(*, session: SessionDep, dataset_id: int, records: list[RecordCreate]):
    get_or_404(session, Dataset, dataset_id)

    db_records = [
        Record.model_validate(record, update={"dataset_id": dataset_id})
        for record in records
    ]
    session.add_all(db_records)
    session.commit()

    return {"ok": True}


@app.patch(
    "/datasets/{dataset_id}", response_model=DatasetReadWithRelations, tags=["Dataset"]
)
def update_dataset(*, session: SessionDep, dataset_id: int, dataset: DatasetUpdate):
    db_dataset = get_or_404(session, Dataset, dataset_id)
    apply_update(db_dataset, dataset, DatasetCreate)
    session.add(db_dataset)
    session.commit()
    session.refresh(db_dataset)
    return db_dataset


@app.delete("/datasets/{dataset_id}", tags=["Dataset"])
def delete_dataset(*, session: SessionDep, dataset_id: int):
    """
    Delete a dataset together with its records. Labelqueues that use the dataset are
    kept and lose their dataset registration. Datasets with tasks cannot be deleted.
    """
    # TODO: delete dataset data file as well
    dataset = get_or_404(session, Dataset, dataset_id)
    ensure_no_tasks(dataset)
    session.delete(dataset)
    session.commit()
    return {"ok": True}


@app.post(
    "/datasets/{dataset_id}/labelqueues/{labelqueue_id}",
    tags=["Dataset"],
)
def register_dataset(*, session: SessionDep, labelqueue_id: int, dataset_id: int):
    """
    Register a dataset to a labelqueue. A dataset may be registered to multiple labelqueues,
    but a labelqueue may only have one dataset. This method raises an error if the labelqueue
    already has a dataset. To change a labelqueue's
    dataset, first unregister the existing dataset, and then register the new dataset.
    """
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)

    if labelqueue.dataset:
        raise HTTPException(
            status_code=406,
            detail="LabelQueue already has a dataset. First remove the existing dataset.",
        )

    dataset = get_or_404(session, Dataset, dataset_id)

    labelqueue.dataset = dataset
    session.add(labelqueue)
    session.commit()
    session.refresh(labelqueue)

    return {"ok": True}


@app.delete("/datasets/{dataset_id}/labelqueues/{labelqueue_id}", tags=["Dataset"])
def unregister_dataset(*, session: SessionDep, labelqueue_id: int, dataset_id: int):
    """
    Unregister a dataset from a labelqueue.
    Retains all completed tasks.
    """
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)
    get_or_404(session, Dataset, dataset_id)

    if labelqueue.dataset is None:
        raise HTTPException(
            status_code=406,
            detail="LabelQueue does not have a registered dataset, so cannot unregister.",
        )

    if labelqueue.dataset.id != dataset_id:
        raise HTTPException(
            status_code=406,
            detail=f"Tried to unregister dataset with ID={dataset_id} but labelqueue with ID={labelqueue_id} has dataset ID={labelqueue.dataset.id}",
        )

    labelqueue.dataset = None
    session.add(labelqueue)
    session.commit()
    session.refresh(labelqueue)

    return {"ok": True}


#
# Records
#
@app.get("/records/{record_id}", response_model=RecordReadWithDataset, tags=["Record"])
def get_record(*, session: SessionDep, record_id: int):
    return get_or_404(session, Record, record_id)


@app.patch(
    "/records/{record_id}", response_model=RecordReadWithDataset, tags=["Record"]
)
def update_record(*, session: SessionDep, record_id: int, record: RecordUpdate):
    db_record = get_or_404(session, Record, record_id)
    apply_update(db_record, record, RecordCreate)
    session.add(db_record)
    session.commit()
    session.refresh(db_record)
    return db_record


@app.delete("/records/{record_id}", tags=["Record"])
def delete_record(*, session: SessionDep, record_id: int):
    record = get_or_404(session, Record, record_id)
    ensure_no_tasks(record)
    session.delete(record)
    session.commit()
    return {"ok": True}


#
# Users
#
@app.post("/users/", response_model=UserReadWithLabelQueues, tags=["User"])
def create_user(*, session: SessionDep, user: UserCreate):
    db_user = User.model_validate(user)
    session.add(db_user)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise HTTPException(400, detail=repr(e))
    session.refresh(db_user)
    return db_user


@app.get("/users/", response_model=list[UserReadWithLabelQueues], tags=["User"])
def get_users(*, session: SessionDep):
    return session.exec(select(User)).all()


@app.get("/users/{user_id}", response_model=UserReadWithLabelQueues, tags=["User"])
def get_user(*, session: SessionDep, user_id: int):
    return get_or_404(session, User, user_id)


@app.patch("/users/{user_id}", response_model=UserReadWithLabelQueues, tags=["User"])
def update_user(*, session: SessionDep, user_id: int, user: UserUpdate):
    db_user = get_or_404(session, User, user_id)
    apply_update(db_user, user, UserCreate)
    session.add(db_user)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        raise HTTPException(400, detail=repr(e))
    session.refresh(db_user)
    return db_user


@app.delete("/users/{user_id}", tags=["User"])
def delete_user(*, session: SessionDep, user_id: int):
    user = get_or_404(session, User, user_id)
    ensure_no_tasks(user)
    session.delete(user)
    session.commit()
    return {"ok": True}


#
# QueueStep
#
@app.get(
    "/queuesteps/{queuestep_id}",
    response_model=QueueStepReadWithLabelQueue,
    tags=["QueueStep"],
)
def get_queuestep(*, session: SessionDep, queuestep_id: int):
    return get_or_404(session, QueueStep, queuestep_id)


@app.patch(
    "/queuesteps/{queuestep_id}",
    response_model=QueueStepReadWithLabelQueue,
    tags=["QueueStep"],
)
def update_queuestep(
    *,
    session: SessionDep,
    queuestep_id: int,
    queuestep: QueueStepUpdate,
):
    db_queuestep = get_or_404(session, QueueStep, queuestep_id)
    # policy_args are re-validated against the (possibly new) step type
    apply_update(db_queuestep, queuestep, QueueStepCreate)
    session.add(db_queuestep)
    session.commit()
    session.refresh(db_queuestep)
    return db_queuestep


@app.delete("/queuesteps/{queuestep_id}", tags=["QueueStep"])
def delete_queuestep(*, session: SessionDep, queuestep_id: int):
    queuestep = get_or_404(session, QueueStep, queuestep_id)
    ensure_no_tasks(queuestep)
    session.delete(queuestep)
    session.commit()
    return {"ok": True}


#
# LabelQueues
#
@app.post(
    "/labelqueues/", response_model=LabelQueueReadWithRelations, tags=["LabelQueue"]
)
def create_labelqueue(*, session: SessionDep, labelqueue: LabelQueueCreate):
    db_labelqueue = LabelQueue.model_validate(labelqueue)
    session.add(db_labelqueue)
    session.commit()
    session.refresh(db_labelqueue)
    return db_labelqueue


@app.get(
    "/labelqueues/",
    response_model=list[LabelQueueReadWithRelations],
    tags=["LabelQueue"],
)
def get_labelqueues(*, session: SessionDep):
    return session.exec(select(LabelQueue)).all()


@app.get(
    "/labelqueues/{labelqueue_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def get_labelqueue(*, session: SessionDep, labelqueue_id: int):
    return get_or_404(session, LabelQueue, labelqueue_id)


@app.patch(
    "/labelqueues/{labelqueue_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def update_labelqueue(
    *,
    session: SessionDep,
    labelqueue_id: int,
    labelqueue: LabelQueueUpdate,
):
    db_labelqueue = get_or_404(session, LabelQueue, labelqueue_id)
    apply_update(db_labelqueue, labelqueue, LabelQueueCreate)
    session.add(db_labelqueue)
    session.commit()
    session.refresh(db_labelqueue)
    return db_labelqueue


@app.delete("/labelqueues/{labelqueue_id}", tags=["LabelQueue"])
def delete_labelqueue(*, session: SessionDep, labelqueue_id: int):
    """
    Delete a labelqueue together with its queue steps. Users and the dataset are kept.
    Labelqueues with tasks cannot be deleted.
    """
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)
    ensure_no_tasks(labelqueue)
    session.delete(labelqueue)
    session.commit()
    return {"ok": True}


@app.post(
    "/labelqueues/{labelqueue_id}/users/{user_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def register_user(*, session: SessionDep, labelqueue_id: int, user_id: int):
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)
    user = get_or_404(session, User, user_id)

    if user.id in {u.id for u in labelqueue.users}:
        raise HTTPException(
            status_code=406, detail="User already present in labelqueue."
        )

    labelqueue.users.append(user)
    session.add(labelqueue)
    session.commit()
    session.refresh(labelqueue)

    return labelqueue


@app.delete(
    "/labelqueues/{labelqueue_id}/users/{user_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def unregister_user(*, session: SessionDep, labelqueue_id: int, user_id: int):
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)
    user = get_or_404(session, User, user_id)

    if user.id not in {u.id for u in labelqueue.users}:
        raise HTTPException(
            status_code=406, detail="User not registered so cannot unregister."
        )

    labelqueue.users = [u for u in labelqueue.users if u.id != user.id]
    session.add(labelqueue)
    session.commit()
    session.refresh(labelqueue)

    return labelqueue


@app.post(
    "/labelqueues/{labelqueue_id}/queue_step/",
    response_model=QueueStepReadWithLabelQueue,
    tags=["LabelQueue", "QueueStep"],
)
def create_queuestep(
    *,
    session: SessionDep,
    labelqueue_id: int,
    queuestep: QueueStepCreate,
):
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)

    # can only create steps at the end
    # a different method will handle changing order
    rank = 1
    if len(labelqueue.queuesteps) > 0:
        rank += max(qs.rank for qs in labelqueue.queuesteps)

    # add fields needed for db then commit
    # (model_dump converts the validated policy_args model into a JSON-compatible dict)
    db_queuestep = QueueStep.model_validate(
        queuestep.model_dump(),
        update={"labelqueue_id": labelqueue_id, "num_records_completed": 0, "rank": rank},
    )
    session.add(db_queuestep)
    session.commit()
    session.refresh(db_queuestep)

    return db_queuestep


@app.post(
    "/labelqueues/{labelqueue_id}/{user_id}/task/",
    response_model=TaskReadWithRelations,
    tags=["LabelQueue"],
)
def create_task(*, session: SessionDep, labelqueue_id: int, user_id: int):
    # check preconditions
    labelqueue = get_or_404(session, LabelQueue, labelqueue_id)

    labelqueue_users = {user.id for user in labelqueue.users}
    if user_id not in labelqueue_users:
        raise HTTPException(
            status_code=404,
            detail="The provided user does not belong to the labelqueue.",
        )

    if labelqueue.dataset is None:
        raise HTTPException(
            status_code=406,
            detail="Cannot create a task because this labelqueue does not have a registered dataset.",
        )

    if len(labelqueue.dataset.records) == 0:
        raise HTTPException(
            status_code=406,
            detail="Cannot create a task because the registered dataset does not have any records.",
        )

    if labelqueue.get_active_queuestep() is None:
        raise HTTPException(
            status_code=406,
            detail="Cannot create a task because this labelqueue does not have an active queue step.",
        )

    # get the next task from the queue
    try:
        next_task: Optional[NextTask] = labelqueue.get_next_task(user_id)
    except Exception as e:
        raise HTTPException(
            status_code=406, detail=f"Unable to get task assignment. Reason: {repr(e)}"
        )

    # TODO: This indicates that the queue is empty (no unassigned records, or the active
    # step is at capacity). There should be a custom object to make this more explicit to the client.
    if next_task is None:
        raise HTTPException(status_code=406, detail="Queue is empty.")

    task = Task(
        record_id=next_task.record_id,
        dataset_id=next_task.dataset_id,
        user_id=user_id,
        queuestep_id=next_task.queuestep_id,
        labelqueue_id=labelqueue_id,
    )

    # add fields needed for db then commit
    session.add(task)
    session.commit()
    session.refresh(task)

    return task
