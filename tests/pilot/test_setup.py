"""setup_pilot.py and export_pilot.py against a real server (``serve_pilot`` in the
conftest), so the scripts' own HTTP code (urllib) is what runs."""

from __future__ import annotations

import csv
import json
import stat
from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="the pilot kit needs Pillow: pip install -e '.[pilot]'")

ROOT = Path(__file__).resolve().parents[2]
ADMIN_PASSWORD = "admin-password-for-tests"


@pytest.fixture
def server(tmp_path: Path, prepared: Path, serve_pilot):
    ontology = ROOT / "pilot" / "jsrt" / "ontology"
    with serve_pilot(tmp_path / "labelflow.db", ontology, prepared / "png") as running:
        yield running


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


def run_setup(setup_module, server, prepared: Path, tmp_path: Path, *extra: str):
    said: list[str] = []
    argv = ["--server", server.url, "--admin", "root", "--items", str(prepared / "items.json")]
    argv += ["--gold", "2", "--credentials", str(tmp_path / "credentials.csv"), *extra]
    status = setup_module.main(argv, password=ADMIN_PASSWORD, say=said.append)
    return status, "\n".join(said)


def admin_api(setup_module, server):
    api = setup_module.Api(server.url)
    api.token = api.post("/auth/login", {"username": "root", "password": ADMIN_PASSWORD})["token"]
    return api


def login(setup_module, server, user: str, password: str):
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
    items = json.loads((prepared / "items.json").read_text())
    first, _ = setup_module.split_items(items, 2, seed=20261011)
    other = next(s for s in range(100) if setup_module.split_items(items, 2, seed=s)[0] != first)
    status, _ = run_setup(setup_module, server, prepared, tmp_path, "--seed", str(other))
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
