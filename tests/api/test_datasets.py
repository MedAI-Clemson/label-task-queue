"""Datasets and items: importing a batch, hashing images, and listing."""

from __future__ import annotations

import hashlib

import pytest

from labelflow.engine import Engine


def test_creating_and_listing_datasets(admin, project, expect):
    created = expect(admin.post(f"{project}/datasets", {"name": "batch2"}), 201)
    assert created == {
        "dataset": "batch2",
        "created_by": "root",
        "created_at": "2026-10-10T09:00:00Z",
        "items": 0,
    }
    listed = expect(admin.get(f"{project}/datasets"))
    assert [(d["dataset"], d["items"]) for d in listed] == [("batch1", 20), ("batch2", 0)]


def test_a_duplicate_dataset_is_a_conflict(admin, project):
    response = admin.post(f"{project}/datasets", {"name": "batch1"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Dataset 'batch1' already exists."


def test_a_bad_dataset_name_is_refused(admin, project):
    response = admin.post(f"{project}/datasets", {"name": "batch 2"})
    assert response.status_code == 422


# --- listing items -----------------------------------------------------------------------


def test_items_are_listed_a_page_at_a_time(admin, project, expect):
    first = expect(admin.get(f"{project}/datasets/batch1/items?limit=5"))
    assert (first["total"], first["limit"], first["offset"]) == (20, 5, 0)
    assert [i["item"] for i in first["rows"]] == [f"img_{n:03d}" for n in range(1, 6)]
    last = expect(admin.get(f"{project}/datasets/batch1/items?limit=5&offset=18"))
    assert [i["item"] for i in last["rows"]] == ["img_019", "img_020"]
    beyond = expect(admin.get(f"{project}/datasets/batch1/items?offset=100"))
    assert beyond["rows"] == []
    assert beyond["total"] == 20


def test_an_item_is_listed_with_everything_recorded_about_it(admin, project, make_items, expect):
    expect(admin.post(f"{project}/datasets", {"name": "gold"}), 201)
    items = make_items(1, gold_key="cat", batch=3)
    expect(admin.post(f"{project}/datasets/gold/items", {"items": items}), 201)
    item = expect(admin.get(f"{project}/datasets/gold/items"))["rows"][0]
    assert item == {
        "dataset": "gold",
        "item": "img_001",
        "uri": "https://images.example.org/img_001.png",
        "content_hash": items[0]["content_hash"],
        "properties": {"gold_key": "cat", "batch": 3},
        "imported_by": "root",
        "imported_at": "2026-10-10T09:00:00Z",
    }


@pytest.mark.parametrize("query", ["limit=0", "limit=1001", "limit=x", "offset=-1"])
def test_a_bad_page_is_refused(admin, project, query):
    response = admin.get(f"{project}/datasets/batch1/items?{query}")
    assert response.status_code == 422
    assert response.json()["detail"].startswith("The request is not valid.")


def test_an_unknown_dataset_is_not_found(admin, project, make_items):
    listing = admin.get(f"{project}/datasets/batch9/items")
    assert listing.status_code == 404
    assert listing.json()["detail"] == "There is no dataset named 'batch9'. Did you mean 'batch1'?"
    importing = admin.post(f"{project}/datasets/nope/items", {"items": make_items(1)})
    assert importing.status_code == 404


# --- hashing at import -------------------------------------------------------------------


def test_the_server_hashes_a_file_inside_the_media_folder(admin, project, media, expect):
    (media / "scans").mkdir()
    (media / "scans" / "b.png").write_bytes(b"pixels of b")
    items = [
        {"item": "as_uri", "uri": (media / "scans" / "b.png").as_uri()},
        {"item": "relative", "uri": "scans/b.png"},
        {"item": "absolute", "uri": str(media / "scans" / "b.png")},
    ]
    expect(admin.post(f"{project}/datasets/batch1/items", {"items": items}), 201)
    stored = expect(admin.get(f"{project}/datasets/batch1/items?offset=20"))["rows"]
    assert {i["content_hash"] for i in stored} == {hashlib.sha256(b"pixels of b").hexdigest()}
    assert [i["uri"] for i in stored] == [i["uri"] for i in items]  # kept as given


def test_a_given_hash_is_kept_and_written_in_lower_case(admin, project, media, expect):
    (media / "c.png").write_bytes(b"pixels of c")
    claimed = "AB" * 32
    item = {"item": "c", "uri": (media / "c.png").as_uri(), "content_hash": claimed}
    expect(admin.post(f"{project}/datasets/batch1/items", {"items": [item]}), 201)
    stored = expect(admin.get(f"{project}/datasets/batch1/items?offset=20"))["rows"][0]
    assert stored["content_hash"] == "ab" * 32  # the server does not check a hash it is given


@pytest.mark.parametrize(
    ("uri", "reason"),
    [
        ("https://images.example.org/x.png", "is not a file on the server"),
        ("s3://bucket/x.png", "is not a file on the server"),
        ("file://other-host/data/x.png", "is a file on another computer"),
        ("file:///etc/hostname", "is outside the server's media folder"),
        ("/etc/hostname", "is outside the server's media folder"),
        ("../outside.png", "is outside the server's media folder"),
        ("missing.png", "no file was found"),
        ("scans", "no file was found"),
    ],
)
def test_an_image_the_server_cannot_hash_is_refused(admin, project, media, uri, reason):
    (media / "scans").mkdir()
    response = admin.post(
        f"{project}/datasets/batch1/items", {"items": [{"item": "x", "uri": uri}]}
    )
    assert response.status_code == 422, uri
    (problem,) = response.json()["problems"]
    assert problem.startswith("Item 'x':")
    assert reason in problem
    assert "Give `content_hash`" in problem


def test_a_link_out_of_the_media_folder_is_refused(admin, project, media, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"secret")
    (media / "link.png").symlink_to(tmp_path / "secret.png")
    item = {"item": "x", "uri": "link.png"}
    response = admin.post(f"{project}/datasets/batch1/items", {"items": [item]})
    assert response.status_code == 422
    assert "outside the server's media folder" in response.json()["problems"][0]


def test_without_a_media_folder_the_server_hashes_nothing(app_with, tmp_path, expect):
    """Default deny: without LABELFLOW_MEDIA_ROOT, every item must bring its hash."""
    root, _ = app_with(media_root=None)
    expect(root.post("/projects", {"name": "xray"}), 201)
    expect(root.post("/projects/xray/datasets", {"name": "d"}), 201)
    (tmp_path / "x.png").write_bytes(b"x")
    items = [
        {"item": "relative", "uri": "x.png"},
        {"item": "absolute", "uri": str(tmp_path / "x.png")},
        {"item": "as_uri", "uri": (tmp_path / "x.png").as_uri()},
    ]
    response = root.post("/projects/xray/datasets/d/items", {"items": items})
    assert response.status_code == 422
    assert len(response.json()["problems"]) == 3
    for problem in response.json()["problems"]:
        assert "the server has no media folder, so it does not hash files" in problem
        assert "Give `content_hash`" in problem
    with_hash = [item | {"content_hash": "ab" * 32} for item in items]
    expect(root.post("/projects/xray/datasets/d/items", {"items": with_hash}), 201)


# --- the batch is all or nothing ---------------------------------------------------------


def test_a_bad_batch_imports_nothing_and_lists_every_problem(
    admin, project, media, make_items, expect
):
    items = [
        *make_items(2, start=100),
        {"item": "web", "uri": "https://example.org/w.png"},
        {"item": "gone", "uri": (media / "gone.png").as_uri()},
        {"item": "img_001", "uri": "https://example.org/dup.png", "content_hash": "0" * 64},
        *make_items(1, start=100),
    ]
    response = admin.post(f"{project}/datasets/batch1/items", {"items": items})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"] == "No items were imported: 4 of 6 have problems."
    assert len(body["problems"]) == 4
    assert "Item 'web'" in body["problems"][0]
    assert "Item 'gone'" in body["problems"][1]
    assert body["problems"][2] == "Item 'img_001' is already in the dataset."
    assert body["problems"][3] == "Item 'img_100' is listed twice."
    assert expect(admin.get(f"{project}/datasets/batch1/items"))["total"] == 20


def test_a_long_list_of_problems_is_cut_short(admin, project):
    items = [{"item": f"web_{n}", "uri": f"https://example.org/{n}.png"} for n in range(130)]
    response = admin.post(f"{project}/datasets/batch1/items", {"items": items})
    body = response.json()
    assert body["detail"] == "No items were imported: 130 of 130 have problems."
    assert len(body["problems"]) == 101
    assert body["problems"][-1] == "...and 30 more."


def test_a_failure_part_way_through_imports_nothing(
    admin, project, make_items, expect, monkeypatch
):
    original = Engine._log
    logged = []

    def flaky(self, kind, actor, **fields):
        if kind.__name__ == "ItemImported":
            logged.append(fields["item"])
            if len(logged) == 3:
                raise RuntimeError("the disk is full")
        return original(self, kind, actor, **fields)

    monkeypatch.setattr(Engine, "_log", flaky)
    with pytest.raises(RuntimeError, match="the disk is full"):
        admin.post(f"{project}/datasets/batch1/items", {"items": make_items(5, start=100)})
    monkeypatch.setattr(Engine, "_log", original)
    assert logged == ["img_100", "img_101", "img_102"]
    assert expect(admin.get(f"{project}/datasets/batch1/items"))["total"] == 20


@pytest.mark.parametrize(
    "item",
    [
        {"item": "", "uri": "x"},
        {"item": "   ", "uri": "x"},
        {"item": "x", "uri": ""},
        {"item": "x", "uri": "u", "content_hash": "not a hash"},
        {"item": "x", "uri": "u", "content_hash": "ab" * 31},
        {"item": "x", "uri": "u", "properties": "gold"},
        {"uri": "u"},
    ],
)
def test_a_malformed_item_is_refused(admin, project, item):
    response = admin.post(f"{project}/datasets/batch1/items", {"items": [item]})
    assert response.status_code == 422, item


def test_an_empty_batch_is_refused(admin, project):
    assert admin.post(f"{project}/datasets/batch1/items", {"items": []}).status_code == 422


def test_items_imported_while_a_workflow_runs_enter_it(
    admin, workflow, project, make_items, expect
):
    expect(admin.post(f"{project}/datasets/batch1/items", {"items": make_items(2, start=50)}), 201)
    progress = expect(admin.get(workflow))["progress"]
    assert progress["items"]["entered"] == 22
    assert progress["stages"][0]["open"] == 22
