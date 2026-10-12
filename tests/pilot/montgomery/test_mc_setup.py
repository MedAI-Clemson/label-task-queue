"""The Montgomery kit's setup_pilot.py and export_pilot.py against a real server, with the
kit's example accounts, as in the README's quick start."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import stat
from pathlib import Path

import pytest

pytest.importorskip("PIL", reason="the pilot kit needs Pillow: pip install -e '.[pilot]'")

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "pilot" / "montgomery"
ADMIN_PASSWORD = "admin-password-for-tests"
BASE = "/projects/montgomery_pilot"


@pytest.fixture
def server(tmp_path: Path, mc_prepared: Path, serve_pilot):
    with serve_pilot(tmp_path / "labelflow.db", KIT / "ontology", mc_prepared / "png") as running:
        yield running


def run_setup(mc_setup, server, mc_prepared: Path, tmp_path: Path):
    said: list[str] = []
    argv = ["--server", server.url, "--admin", "root", "--items", str(mc_prepared / "items.json")]
    argv += ["--gold", "2", "--accounts", str(KIT / "accounts.example.csv")]
    argv += ["--credentials", str(tmp_path / "out" / "credentials.csv")]
    status = mc_setup.main(argv, password=ADMIN_PASSWORD, say=said.append)
    return status, "\n".join(said)


def login(mc_setup, server, user: str, password: str):
    api = mc_setup.Api(server.url)
    api.login(user, password)
    return api


def test_setup_with_the_example_accounts_then_label_and_export(
    mc_setup, mc_export, server, mc_prepared, tmp_path
):
    status, said = run_setup(mc_setup, server, mc_prepared, tmp_path)
    assert status == 0, said
    assert "workflow montgomery_training_1: started with seed 20261011" in said
    assert "workflow montgomery_main: started with seed 20261011" in said
    assert "account pilot_admin: created (administrator)" in said
    assert "WARNING: these are live passwords" in said

    admin = login(mc_setup, server, "root", ADMIN_PASSWORD)
    roles = {r["role"]: r["holders"] for r in admin.get(f"{BASE}/roles")}
    assert roles == {
        "trainee": ["trainee_one", "trainee_three", "trainee_two"],
        "annotator": ["annotator_one", "annotator_three", "annotator_two"],
        "expert": ["expert_one", "expert_two"],
    }
    assert sorted(a["user_id"] for a in admin.get("/accounts") if a["is_admin"]) == [
        "pilot_admin",
        "root",
    ]
    gold = admin.get(f"{BASE}/datasets/montgomery_gold/items")["rows"]
    main = admin.get(f"{BASE}/datasets/montgomery_main/items")["rows"]
    assert len(gold) == 2 and len(main) == 2
    assert sorted(i["properties"]["tb_findings"] for i in gold) == ["absent", "present"]
    progress = admin.get(f"{BASE}/workflows/montgomery_training_1")["progress"]
    assert progress["items"]["entered"] == 2
    assert progress["stages"][0]["open"] == 2 * 3  # every trainee, every gold image

    # every password was generated, written for the admin only, and works
    credentials = tmp_path / "out" / "credentials.csv"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    with credentials.open(newline="") as handle:
        passwords = {row["username"]: row["password"] for row in csv.DictReader(handle)}
    assert len(passwords) == 9 and len(set(passwords.values())) == 9
    login(mc_setup, server, "pilot_admin", passwords["pilot_admin"])

    # a trainee gets work, the image route serves the hashed PNG, and nothing gives the
    # answer away; the first label is the key, the second has the wrong TB answer
    keys = {
        i["item"]: i["properties"]["gold_key"]
        for i in json.loads((mc_prepared / "items.json").read_text())
    }
    trainee = login(mc_setup, server, "trainee_one", passwords["trainee_one"])
    for wrong in (False, True):
        work = trainee.post(f"{BASE}/workflows/montgomery_training_1/next")
        slot, packet = work["slot"]["slot"], work["packet"]
        image = trainee.fetch(f"/ui{BASE}/workflows/montgomery_training_1/slots/{slot}/image")
        assert hashlib.sha256(image).hexdigest() == packet["content_hash"]
        assert "gold_key" not in json.dumps(work) and "MCUCXR" not in json.dumps(work)
        label = copy.deepcopy(keys[packet["item"]])
        if wrong:
            flip = {"present": "absent", "absent": "present"}
            label["classifications"]["tb_findings"] = flip[label["classifications"]["tb_findings"]]
        body = {"label": label, "shown": work["shown"], "duration_ms": 60000}
        trainee.post(f"{BASE}/workflows/montgomery_training_1/slots/{slot}/submit", body)
    scores = admin.get(f"{BASE}/workflows/montgomery_training_1/reports/scores")
    assert [(s["labeler"], s["scored"], s["agreed"]) for s in scores] == [("trainee_one", 2, 1)]

    annotator = login(mc_setup, server, "annotator_one", passwords["annotator_one"])
    assert annotator.post(f"{BASE}/workflows/montgomery_main/next")["packet"]["stage"] == "annotate"

    # again: nothing new, and no password is generated twice
    status, said = run_setup(mc_setup, server, mc_prepared, tmp_path)
    assert status == 0, said
    assert "workflow montgomery_main: already running" in said and "created" not in said
    with credentials.open(newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 9

    said_export: list[str] = []
    out = tmp_path / "exports"
    argv = ["--server", server.url, "--admin", "root", "--out", str(out)]
    assert mc_export.main(argv, password=ADMIN_PASSWORD, say=said_export.append) == 0
    with (out / "montgomery_training_1" / "structure_scores.csv").open(newline="") as handle:
        rows = {r["field"]: r for r in csv.DictReader(handle)}
    assert set(rows) == {"tb_findings", "right_lung", "left_lung"}
    assert (rows["tb_findings"]["scored"], rows["tb_findings"]["agreed"]) == ("2", "1")
    assert (rows["right_lung"]["agreed"], rows["right_lung"]["mean_iou"]) == ("2", "1.0")
    assert len(list((out / "montgomery_main" / "explain").glob("*.txt"))) == 2
