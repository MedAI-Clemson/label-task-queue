"""setup_pilot.py and export_pilot.py against a real server: the app from ``create_app``
on a migrated temporary database, served by uvicorn in a thread, so the scripts' own HTTP
code (urllib) is what runs."""

from __future__ import annotations

import csv
import json
import socket
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from alembic import command
from alembic.config import Config

from labelflow.api import Settings, create_app

pytest.importorskip("PIL", reason="the pilot kit needs Pillow: pip install -e '.[pilot]'")

ROOT = Path(__file__).resolve().parents[2]
ADMIN_PASSWORD = "admin-password-for-tests"


@dataclass
class Server:
    url: str
    app: object


@pytest.fixture
def server(tmp_path: Path, prepared: Path) -> Iterator[Server]:
    database = tmp_path / "labelflow.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    settings = Settings(
        database_uri=f"sqlite:///{database}",
        label_tasks=ROOT / "pilot" / "jsrt" / "ontology",  # the pilot's ontology folder as is
        media_root=prepared / "png",
        sqlite_synchronous="OFF",
        password_cost=4,
    )
    app = create_app(settings)
    runtime = app.state.runtime
    runtime.ready()
    runtime.accounts.create("root", ADMIN_PASSWORD, "Root", is_admin=True)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uv.started:
        assert time.monotonic() < deadline, "the server did not start"
        time.sleep(0.02)
    try:
        yield Server(f"http://127.0.0.1:{port}", app)
    finally:
        uv.should_exit = True
        thread.join(10)
        sock.close()


@pytest.fixture
def accounts_csv(tmp_path: Path) -> Path:
    path = tmp_path / "people.csv"
    path.write_text(
        "username,display_name,roles,password\n"
        "tara,Tara,trainee,\n"
        "tom,Tom,trainee,a-password-for-tom\n"
        "ana,Ana,annotator,\n"
        "ben,Ben,annotator,\n"
        "dr_eve,Dr Eve,expert,\n"
        "dr_fay,Dr Fay,expert;annotator,\n"
    )
    return path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def run_setup(setup_module, server: Server, prepared: Path, tmp_path: Path, *extra: str):
    said: list[str] = []
    argv = ["--server", server.url, "--admin", "root", "--items", str(prepared / "items.json")]
    argv += ["--gold", "2", "--credentials", str(tmp_path / "credentials.csv"), *extra]
    status = setup_module.main(argv, password=ADMIN_PASSWORD, say=said.append)
    return status, "\n".join(said)


def admin_api(setup_module, server: Server):
    api = setup_module.Api(server.url)
    api.token = api.post("/auth/login", {"username": "root", "password": ADMIN_PASSWORD})["token"]
    return api


def login(setup_module, server: Server, user: str, password: str):
    api = setup_module.Api(server.url)
    api.token = api.post("/auth/login", {"username": user, "password": password})["token"]
    return api


def test_setup_creates_everything_and_starts_both_workflows(
    setup_module, server, prepared, tmp_path, accounts_csv
):
    status, said = run_setup(
        setup_module, server, prepared, tmp_path, "--accounts", str(accounts_csv)
    )
    assert status == 0, said
    assert "workflow jsrt_training_1: started with seed 20261011" in said
    assert "workflow jsrt_main: started with seed 20261011" in said
    assert "Next steps:" in said

    api = admin_api(setup_module, server)
    base = "/projects/jsrt_pilot"
    roles = {r["role"]: r["holders"] for r in api.get(f"{base}/roles")}
    assert roles == {
        "trainee": ["tara", "tom"],
        "annotator": ["ana", "ben", "dr_fay"],
        "expert": ["dr_eve", "dr_fay"],
    }
    datasets = {d["dataset"]: d for d in api.get(f"{base}/datasets")}
    gold = api.get(f"{base}/datasets/jsrt_gold/items")["rows"]
    main = api.get(f"{base}/datasets/jsrt_main/items")["rows"]
    assert len(gold) == 2 and len(main) == 3 and set(datasets) == {"jsrt_gold", "jsrt_main"}
    # stratified: one image with a nodule and one without
    assert sorted(i["properties"]["nodule"] for i in gold) == ["absent", "present"]
    items = {i["item"]: i for i in json.loads((prepared / "items.json").read_text())}
    for row in gold + main:
        assert row["content_hash"] == items[row["item"]]["content_hash"]
        assert row["properties"]["gold_key"] == items[row["item"]]["properties"]["gold_key"]

    workflows = {w["workflow"]: w for w in api.get(f"{base}/workflows")}
    assert set(workflows) == {"jsrt_training_1", "jsrt_main"}
    assert {w["label_task"] for w in workflows.values()} == {"jsrt_scr@1"}
    assert {w["seed"] for w in workflows.values()} == {20261011}

    # generated passwords: written for the admin only; the given one is not repeated
    credentials = tmp_path / "credentials.csv"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    rows = read_csv(credentials)
    assert [r["username"] for r in rows] == ["tara", "ana", "ben", "dr_eve", "dr_fay"]
    login(setup_module, server, "tara", rows[0]["password"])
    login(setup_module, server, "tom", "a-password-for-tom")

    # again: nothing new
    status, said = run_setup(
        setup_module, server, prepared, tmp_path, "--accounts", str(accounts_csv)
    )
    assert status == 0, said
    assert "workflow jsrt_main: already running" in said
    assert "0 items imported, 3 in all" in said and "created" not in said
    assert len(read_csv(credentials)) == 5


def test_the_training_round_waits_for_trainees(
    setup_module, server, prepared, tmp_path, accounts_csv
):
    status, said = run_setup(setup_module, server, prepared, tmp_path)
    assert status == 0, said
    assert "workflow jsrt_training_1: NOT started, because nobody holds 'trainee'" in said
    assert "workflow jsrt_main: started" in said
    # the validator's staffing warnings are passed on
    assert "warning" in said and "'annotate'" in said

    status, said = run_setup(
        setup_module, server, prepared, tmp_path, "--accounts", str(accounts_csv)
    )
    assert status == 0, said
    assert "workflow jsrt_training_1: started" in said


def test_a_different_split_on_a_set_up_dataset_is_refused(setup_module, server, prepared, tmp_path):
    assert run_setup(setup_module, server, prepared, tmp_path)[0] == 0
    status, _ = run_setup(setup_module, server, prepared, tmp_path, "--seed", "7")
    assert status == 1


def test_scoring_against_the_gold_key_end_to_end(
    setup_module, export_module, server, prepared, tmp_path, accounts_csv
):
    assert (
        run_setup(setup_module, server, prepared, tmp_path, "--accounts", str(accounts_csv))[0] == 0
    )
    base = "/projects/jsrt_pilot/workflows"
    keys = {
        i["item"]: i["properties"]["gold_key"]
        for i in json.loads((prepared / "items.json").read_text())
    }
    tom = login(setup_module, server, "tom", "a-password-for-tom")
    # Tom labels both training images: the first exactly as the key, the second with the
    # wrong nodule answer.
    for answer in ("same", "wrong nodule"):
        work = tom.post(f"{base}/jsrt_training_1/next")
        label = json.loads(json.dumps(keys[work["slot"]["item"]]))
        if answer == "wrong nodule":
            flip = {"present": "absent", "absent": "present"}
            label["classifications"]["nodule"] = flip[label["classifications"]["nodule"]]
        body = {"label": label, "shown": work["shown"], "duration_ms": 60000}
        tom.post(f"{base}/jsrt_training_1/slots/{work['slot']['slot']}/submit", body)
        # the image the server serves is the PNG that was hashed
        packet = work["packet"]
        assert packet["content_hash"] == work["shown"]["content_hash"]
        assert "gold_key" not in json.dumps(packet)  # never shown to the labeler

    api = admin_api(setup_module, server)
    scores = api.get(f"{base}/jsrt_training_1/reports/scores")
    assert [(s["labeler"], s["scored"], s["agreed"]) for s in scores] == [("tom", 2, 1)]

    said: list[str] = []
    out = tmp_path / "exports"
    argv = ["--server", server.url, "--admin", "root", "--out", str(out)]
    assert export_module.main(argv, password=ADMIN_PASSWORD, say=said.append) == 0, said
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    training = out / "jsrt_training_1"
    for name in ("items.csv", "items.parquet", "labels.csv", "labels.parquet", "scores.json"):
        assert (training / name).stat().st_size > 0
    assert sorted(p.stem for p in (training / "explain").glob("*.txt")) == sorted(
        r["item"] for r in api.get("/projects/jsrt_pilot/datasets/jsrt_gold/items")["rows"]
    )
    rows = read_csv(training / "structure_scores.csv")
    by_field = {r["field"]: r for r in rows}
    assert set(by_field) == {
        "nodule",
        "right_lung",
        "left_lung",
        "heart",
        "right_clavicle",
        "left_clavicle",
    }
    assert (by_field["nodule"]["scored"], by_field["nodule"]["agreed"]) == ("2", "1")
    assert (by_field["heart"]["agreed"], by_field["heart"]["mean_iou"]) == ("2", "1.0")
    labels = read_csv(training / "labels.csv")
    assert {r["roles_at_claim"] for r in labels} == {'["trainee"]'}
