import os

from fastapi import FastAPI, HTTPException, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from typing import List

from database import create_db_and_tables, get_session
from models import *

app = FastAPI(swagger_ui_parameters={"tryItOutEnabled": "true"})


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


#
# Datasets
#
@app.post("/datasets/", response_model=DatasetReadWithRelations, tags=["Dataset"])
def create_dataset(*, session: Session = Depends(get_session), dataset: DatasetCreate):
    db_dataset = Dataset.from_orm(dataset)
    session.add(db_dataset)
    session.commit()
    session.refresh(db_dataset)
    return DatasetReadWithRelations.from_orm(db_dataset)


@app.get("/datasets/", response_model=List[DatasetReadWithRelations], tags=["Dataset"])
def get_datasets(*, session: Session = Depends(get_session)):
    datasets = session.exec(select(Dataset)).all()
    return datasets


@app.get(
    "/datasets/{dataset_id}", response_model=DatasetReadWithRelations, tags=["Dataset"]
)
def get_dataset(*, session: Session = Depends(get_session), dataset_id):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


@app.post("/dataset/{dataset_id}/records/", tags=["Dataset"])
def create_records(
    *, session: Session = Depends(get_session), dataset_id, records: List[RecordCreate]
):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    db_records = []
    for record in records:
        it = Record.from_orm(record)
        it.dataset_id = dataset_id
        db_records.append(it)
    session.add_all(db_records)
    session.commit()

    return {"ok": True}


@app.patch(
    "/datasets/{dataset_id}", response_model=DatasetReadWithRelations, tags=["Dataset"]
)
def update_dataset(
    *, session: Session = Depends(get_session), dataset_id: int, dataset: DatasetUpdate
):
    db_dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    dataset_dict = dataset.dict(exclude_unset=True)
    for k, v in dataset_dict.items():
        setattr(db_dataset, k, v)
    session.add(db_dataset)
    session.commit()
    session.refresh(db_dataset)
    return db_dataset


@app.delete("/datasets/{dataset_id}", tags=["Dataset"])
def delete_dataset(*, session: Session = Depends(get_session), dataset_id: int):
    # TODO: delete dataset data file as well
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    session.delete(dataset)
    session.commit()
    return {"ok": True}


@app.post(
    "/datasets/{dataset_id}/labelqueues/{labelqueue_id}",
    tags=["Dataset"],
)
def register_dataset(
    *, session: Session = Depends(get_session), labelqueue_id: int, dataset_id
):
    """
    Register a dataset to a labelqueue. A dataset may be registered to multiple labelqueues,
    but a labelqueue may only have one dataset. This method raises an error if the labelqueue
    already has a dataset. To change a labelqueue's
    dataset, first unregister the existing dataset, and then register the new dataset.
    """
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    if labelqueue.dataset:
        raise HTTPException(
            status_code=406,
            detail="LabelQueue already has a dataset. First remove the existing dataset.",
        )

    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    labelqueue.dataset = dataset
    session.add(labelqueue)
    session.commit()
    session.refresh(labelqueue)

    return {"ok": True}


@app.delete("/datasets/{dataset_id}/labelqueues/{labelqueue_id}", tags=["Dataset"])
def unregister_dataset(
    *, session: Session = Depends(get_session), labelqueue_id: int, dataset_id: int
):
    """
    Unregister a dataset from a labelqueue.
    Retains all completed tasks.
    """
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found.")

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
def get_record(*, session: Session = Depends(get_session), record_id):
    record = session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    return record


@app.patch(
    "/records/{record_id}", response_model=RecordReadWithDataset, tags=["Record"]
)
def update_record(
    *, session: Session = Depends(get_session), record_id: int, record: RecordUpdate
):
    db_record = session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")

    record_dict = record.dict(exclude_unset=True)
    for k, v in record_dict.items():
        setattr(db_record, k, v)
    session.add(db_record)
    session.commit()
    session.refresh(db_record)
    return db_record


@app.delete("/records/{record_id}", tags=["Record"])
def delete_record(*, session: Session = Depends(get_session), record_id: int):
    record = session.get(Record, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    session.delete(record)
    session.commit()
    return {"ok": True}


#
# Users
#
@app.post("/users/", response_model=UserReadWithLabelQueues, tags=["User"])
def create_user(*, session: Session = Depends(get_session), user: UserCreate):
    db_user = User.from_orm(user)
    session.add(db_user)
    try:
        session.commit()
    except Exception as e:
        raise HTTPException(400, detail=repr(e))
    session.refresh(db_user)
    return db_user


@app.get("/users/", response_model=List[UserReadWithLabelQueues], tags=["User"])
def get_users(*, session: Session = Depends(get_session)):
    users = session.exec(select(User)).all()
    return users


@app.get("/users/{user_id}", response_model=UserReadWithLabelQueues, tags=["User"])
def get_user(*, session: Session = Depends(get_session), user_id):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.patch("/users/{user_id}", response_model=UserReadWithLabelQueues, tags=["User"])
def update_user(
    *, session: Session = Depends(get_session), user_id: int, user: UserUpdate
):
    db_user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_dict = user.dict(exclude_unset=True)
    for k, v in user_dict.items():
        setattr(db_user, k, v)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return db_user


@app.delete("/users/{user_id}", tags=["User"])
def delete_user(*, session: Session = Depends(get_session), user_id: int):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
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
def get_queuestep(*, session: Session = Depends(get_session), queuestep_id):
    queuestep = session.get(QueueStep, queuestep_id)
    if not queuestep:
        raise HTTPException(status_code=404, detail="QueueStep not found")
    return queuestep


@app.patch(
    "/queuesteps/{queuestep_id}",
    response_model=QueueStepReadWithLabelQueue,
    tags=["QueueStep"],
)
def update_queuestep(
    *,
    session: Session = Depends(get_session),
    queuestep_id: int,
    queuestep: QueueStepUpdate,
):
    db_queuestep = session.get(QueueStep, queuestep_id)
    if not queuestep:
        raise HTTPException(status_code=404, detail="QueueStep not found")

    queuestep_dict = queuestep.dict(exclude_unset=True)
    for k, v in queuestep_dict.items():
        setattr(db_queuestep, k, v)
    session.add(db_queuestep)
    session.commit()
    session.refresh(db_queuestep)
    return db_queuestep


@app.delete("/queuesteps/{queuestep_id}", tags=["QueueStep"])
def delete_queuestep(*, session: Session = Depends(get_session), queuestep_id: int):
    queuestep = session.get(QueueStep, queuestep_id)
    if not queuestep:
        raise HTTPException(status_code=404, detail="QueueStep not found")
    session.delete(queuestep)
    session.commit()
    return {"ok": True}


#
# LabelQueues
#
@app.post(
    "/labelqueues/", response_model=LabelQueueReadWithRelations, tags=["LabelQueue"]
)
def create_labelqueue(
    *, session: Session = Depends(get_session), labelqueue: LabelQueueCreate
):
    db_labelqueue = LabelQueue.from_orm(labelqueue)
    session.add(db_labelqueue)
    session.commit()
    session.refresh(db_labelqueue)
    return db_labelqueue


@app.get(
    "/labelqueues/",
    response_model=List[LabelQueueReadWithRelations],
    tags=["LabelQueue"],
)
def get_labelqueues(*, session: Session = Depends(get_session)):
    labelqueues = session.exec(select(LabelQueue)).all()
    return labelqueues


@app.get(
    "/labelqueues/{labelqueue_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def get_labelqueue(*, session: Session = Depends(get_session), labelqueue_id):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")
    return labelqueue


@app.patch(
    "/labelqueues/{labelqueue_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def update_labelqueue(
    *,
    session: Session = Depends(get_session),
    labelqueue_id: int,
    labelqueue: LabelQueueUpdate,
):
    db_labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    labelqueue_dict = labelqueue.dict(exclude_unset=True)
    for k, v in labelqueue_dict.items():
        setattr(db_labelqueue, k, v)
    session.add(db_labelqueue)
    session.commit()
    session.refresh(db_labelqueue)
    return db_labelqueue


@app.delete("/labelqueues/{labelqueue_id}", tags=["LabelQueue"])
def delete_labelqueue(*, session: Session = Depends(get_session), labelqueue_id: int):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")
    session.delete(labelqueue)
    session.commit()
    return {"ok": True}


@app.post(
    "/labelqueues/{labelqueue_id}/users/{user_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def register_user(
    *, session: Session = Depends(get_session), labelqueue_id: int, user_id
):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id not in {ts.id for ts in labelqueue.users}:
        labelqueue.users.append(user)
        session.add(labelqueue)
        session.commit()
        session.refresh(labelqueue)
    else:
        raise HTTPException(
            status_code=406, detail="User already present in labelqueue."
        )

    return labelqueue


@app.delete(
    "/labelqueues/{labelqueue_id}/users/{user_id}",
    response_model=LabelQueueReadWithRelations,
    tags=["LabelQueue"],
)
def unregister_user(
    *, session: Session = Depends(get_session), labelqueue_id: int, user_id
):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id in {ts.id for ts in labelqueue.users}:
        labelqueue.users = [ts for ts in labelqueue.users if ts.id != user.id]
        session.add(labelqueue)
        session.commit()
        session.refresh(labelqueue)
    else:
        raise HTTPException(
            status_code=406, detail="User not registered so cannot unregister."
        )

    return labelqueue


@app.post(
    "/labelqueues/{labelqueue_id}/queue_step/",
    response_model=QueueStepReadWithLabelQueue,
    tags=["LabelQueue", "QueueStep"],
)
def create_queuestep(
    *,
    session: Session = Depends(get_session),
    labelqueue_id,
    queuestep: QueueStepCreate,
):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    # can only create steps at the end
    # a different method will handle changing order
    rank = 1
    if len(labelqueue.queuesteps) > 0:
        rank += max(labelqueue.queuesteps, key=lambda x: x.rank).rank

    # add fields needed for db then commit
    queuestep = QueueStep.from_orm(queuestep)
    queuestep.labelqueue_id = labelqueue_id
    queuestep.num_records_completed = 0
    queuestep.rank = rank
    session.add(queuestep)
    session.commit()
    session.refresh(queuestep)

    return queuestep


@app.post(
    "/labelqueues/{labelqueue_id}/{user_id}/task/",
    response_model=TaskReadWithRelations,
    tags=["LabelQueue"],
)
def create_task(
    *, session: Session = Depends(get_session), labelqueue_id: int, user_id: int
):
    labelqueue = session.get(LabelQueue, labelqueue_id)
    if not labelqueue:
        raise HTTPException(status_code=404, detail="LabelQueue not found")

    labelqueue_users = {user.id for user in labelqueue.users}
    if user_id not in labelqueue_users:
        raise HTTPException(
            status_code=404, detail="User does not belong to labelqueue"
        )

    # get the next task from the queue
    try:
        next_task: NextTask = labelqueue.get_next_assignment(user_id)
    except Exception as e:
        raise HTTPException(
            status_code=404, detail=f"Unable to get task assignment. Reason: {repr(e)}"
        )

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

    return TaskReadWithRelations.from_orm(task)
