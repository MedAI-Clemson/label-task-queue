import os

from fastapi import FastAPI, HTTPException, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from typing import List

from database import create_db_and_tables, get_session
from models import Taskset, TasksetCreate, TasksetReadWithProjects, TasksetUpdate
from models import (
    Project,
    ProjectCreate,
    ProjectReadWithTasksetsAndUsers,
    ProjectUpdate,
)
from models import User, UserCreate, UserReadWithProjects, UserUpdate


##
## Routes
app = FastAPI()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


@app.post("/tasksets/", response_model=TasksetReadWithProjects, tags=["Taskset"])
def create_taskset(*, session: Session = Depends(get_session), taskset: TasksetCreate):
    db_taskset = Taskset.from_orm(taskset)
    session.add(db_taskset)
    session.commit()
    session.refresh(db_taskset)
    return db_taskset


@app.get("/tasksets/", response_model=List[TasksetReadWithProjects], tags=["Taskset"])
def get_tasksets(*, session: Session = Depends(get_session)):
    tasksets = session.exec(select(Taskset)).all()
    return tasksets


@app.get(
    "/tasksets/{taskset_id}", response_model=TasksetReadWithProjects, tags=["Taskset"]
)
def get_taskset(*, session: Session = Depends(get_session), taskset_id):
    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")
    return taskset


@app.patch(
    "/tasksets/{taskset_id}", response_model=TasksetReadWithProjects, tags=["Taskset"]
)
def update_taskset(
    *, session: Session = Depends(get_session), taskset_id: int, taskset: TasksetUpdate
):
    db_taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")

    taskset_dict = taskset.dict(exclude_unset=True)
    for k, v in taskset_dict.items():
        setattr(db_taskset, k, v)
    session.add(db_taskset)
    session.commit()
    session.refresh(db_taskset)
    return db_taskset


@app.delete("/tasksets/{taskset_id}", tags=["Taskset"])
def delete_taskset(*, session: Session = Depends(get_session), taskset_id: int):
    # TODO: delete taskset data file as well
    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")
    session.delete(taskset)
    session.commit()
    return {"ok": True}


# Tasksets
@app.post("/tasksets/{taskset_id}/data", tags=["Taskset"])
def upload_data_to_taskset(
    *,
    session: Session = Depends(get_session),
    taskset_id: int,
    file: UploadFile = File(...),
):
    # TODO: add file object abstraction in order to store metadata like media type and filename
    # TODO: add upload file validation
    # TODO: return computed data summary statistics

    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")
    if taskset.data_file:
        raise HTTPException(
            400,
            "Taskset already has an attached datafile. First delete the current data.",
        )

    # load and save the file
    dir = f"data/tasksets/{taskset_id}"
    filename = f"{dir}/{file.filename}"
    try:
        if not os.path.exists(dir):
            os.makedirs(dir)

        with open(filename, "wb") as f:
            while contents := file.file.read(1024 * 1024):
                f.write(contents)
    except Exception as e:
        raise HTTPException(500, repr(e))

    # update the taskset record to include file path
    taskset.data_file = filename
    session.add(taskset)
    session.commit()
    session.refresh(taskset)

    return {"message": f"Successfully uploaded {file.filename} {file.headers}"}


# Tasksets
@app.get("/tasksets/{taskset_id}/data", response_class=FileResponse, tags=["Taskset"])
def download_data_from_taskset(
    *, session: Session = Depends(get_session), taskset_id: int
):
    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")
    if not taskset.data_file:
        raise HTTPException(
            status_code=404, detail="Taskset does not have a data file."
        )

    return FileResponse(taskset.data_file)


# Tasksets
@app.delete("/tasksets/{taskset_id}/data", tags=["Taskset"])
def delete_data_from_taskset(
    *, session: Session = Depends(get_session), taskset_id: int
):
    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")
    if not taskset.data_file:
        raise HTTPException(
            status_code=404, detail="Taskset does not have a data file."
        )

    try:
        os.remove(taskset.data_file)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Unable to delete data from taskset. {repr(e)}"
        )

    taskset.data_file = None
    session.add(taskset)
    session.commit()
    session.refresh(taskset)

    return {"ok": True}


# Users
@app.post("/users/", response_model=UserReadWithProjects, tags=["User"])
def create_user(*, session: Session = Depends(get_session), user: UserCreate):
    db_user = User.from_orm(user)
    session.add(db_user)
    try:
        session.commit()
    except Exception as e:
        raise HTTPException(400, detail=repr(e))
    session.refresh(db_user)
    return db_user


@app.get("/users/", response_model=List[UserReadWithProjects], tags=["User"])
def get_users(*, session: Session = Depends(get_session)):
    users = session.exec(select(User)).all()
    return users


@app.get("/users/{user_id}", response_model=UserReadWithProjects, tags=["User"])
def get_user(*, session: Session = Depends(get_session), user_id):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.patch("/users/{user_id}", response_model=UserReadWithProjects, tags=["User"])
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


# Projects
@app.post(
    "/projects/", response_model=ProjectReadWithTasksetsAndUsers, tags=["Project"]
)
def create_project(*, session: Session = Depends(get_session), project: ProjectCreate):
    db_project = Project.from_orm(project)
    session.add(db_project)
    session.commit()
    session.refresh(db_project)
    return db_project


@app.get(
    "/projects/", response_model=List[ProjectReadWithTasksetsAndUsers], tags=["Project"]
)
def get_projects(*, session: Session = Depends(get_session)):
    projects = session.exec(select(Project)).all()
    return projects


@app.get(
    "/projects/{project_id}",
    response_model=ProjectReadWithTasksetsAndUsers,
    tags=["Project"],
)
def get_project(*, session: Session = Depends(get_session), project_id):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.patch(
    "/projects/{project_id}",
    response_model=ProjectReadWithTasksetsAndUsers,
    tags=["Project"],
)
def update_project(
    *, session: Session = Depends(get_session), project_id: int, project: ProjectUpdate
):
    db_project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dict = project.dict(exclude_unset=True)
    for k, v in project_dict.items():
        setattr(db_project, k, v)
    session.add(db_project)
    session.commit()
    session.refresh(db_project)
    return db_project


@app.delete("/projects/{project_id}", tags=["Project"])
def delete_project(*, session: Session = Depends(get_session), project_id: int):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    session.delete(project)
    session.commit()
    return {"ok": True}


@app.post(
    "/projects/{project_id}/tasksets/{taskset_id}",
    response_model=ProjectReadWithTasksetsAndUsers,
    tags=["Project"],
)
def register_taskset(
    *, session: Session = Depends(get_session), project_id: int, taskset_id
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")

    if taskset.id not in {ts.id for ts in project.tasksets}:
        project.tasksets.append(taskset)
        session.add(project)
        session.commit()
        session.refresh(project)
    else:
        raise HTTPException(
            status_code=406, detail="Taskset already present in project."
        )

    return project


@app.delete("/projects/{project_id}/tasksets/{taskset_id}", tags=["Project"])
def unregister_taskset(
    *, session: Session = Depends(get_session), project_id: int, taskset_id
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    taskset = session.get(Taskset, taskset_id)
    if not taskset:
        raise HTTPException(status_code=404, detail="Taskset not found")

    if taskset.id in {ts.id for ts in project.tasksets}:
        project.tasksets = [ts for ts in project.tasksets if ts.id != taskset.id]
        session.add(project)
        session.commit()
        session.refresh(project)
    else:
        raise HTTPException(
            status_code=406, detail="Taskset not registered so cannot unregister."
        )

    return project


@app.post(
    "/projects/{project_id}/users/{user_id}",
    response_model=ProjectReadWithTasksetsAndUsers,
    tags=["Project"],
)
def register_user(*, session: Session = Depends(get_session), project_id: int, user_id):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id not in {ts.id for ts in project.users}:
        project.users.append(user)
        session.add(project)
        session.commit()
        session.refresh(project)
    else:
        raise HTTPException(status_code=406, detail="User already present in project.")

    return project


@app.delete(
    "/projects/{project_id}/users/{user_id}",
    response_model=ProjectReadWithTasksetsAndUsers,
    tags=["Project"],
)
def unregister_user(
    *, session: Session = Depends(get_session), project_id: int, user_id
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id in {ts.id for ts in project.users}:
        project.users = [ts for ts in project.users if ts.id != user.id]
        session.add(project)
        session.commit()
        session.refresh(project)
    else:
        raise HTTPException(
            status_code=406, detail="User not registered so cannot unregister."
        )

    return project
