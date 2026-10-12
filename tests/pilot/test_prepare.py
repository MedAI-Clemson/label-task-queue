"""prepare.py, end to end on synthetic data in the real JSRT and SCR formats."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from labelflow.labeltask import load_ontology, rle

Image = pytest.importorskip(
    "PIL.Image", reason="the pilot kit needs Pillow: pip install -e '.[pilot]'"
)

ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY = ROOT / "pilot" / "jsrt" / "ontology" / "jsrt_scr.yaml"
STRUCTURES = ["right_lung", "left_lung", "heart", "right_clavicle", "left_clavicle"]


@pytest.fixture(scope="module")
def items(prepared: Path) -> list[dict]:
    return json.loads((prepared / "items.json").read_text())


@pytest.fixture(scope="module")
def by_source(items: list[dict]) -> dict[str, dict]:
    return {item["properties"]["source_id"]: item for item in items}


def test_every_image_becomes_an_item_with_its_png_and_hash(prepared, items, by_source, fake_jsrt):
    assert sorted(by_source) == fake_jsrt.ids
    for item in items:
        png = prepared / "png" / f"{item['item']}.png"
        assert item["uri"] == png.resolve().as_uri()
        assert item["content_hash"] == hashlib.sha256(png.read_bytes()).hexdigest()
        with Image.open(png) as image:
            assert image.format == "PNG"
            assert image.mode == "L"
            assert image.size == (1024, 1024)


def test_item_names_do_not_give_the_nodule_answer_away(items, by_source, fake_jsrt):
    for source_id, item in by_source.items():
        source = fake_jsrt.jsrt / "images" / f"{source_id}.IMG"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert item["item"] == f"jsrt_{digest[:10]}"
        assert item["properties"]["source_sha256"] == digest
        assert "JPC" not in item["item"] and "JPC" not in item["uri"]
    # listed in the order of the opaque names, not the JSRT's (nodule cases first)
    assert [i["item"] for i in items] == sorted(i["item"] for i in items)


def test_the_png_is_downsampled_inverted_and_windowed(prepared, by_source, fake_jsrt):
    raw = fake_jsrt.raw(0).astype(np.float64)
    with Image.open(prepared / "png" / f"{by_source['JPCLN001']['item']}.png") as image:
        pixels = np.asarray(image)
    # 2 x 2 block means of the inverted image, windowed at its 0.5 and 99.5 percentiles
    blocks = 4095 - raw.reshape(1024, 2, 1024, 2).mean(axis=(1, 3))
    low, high = np.percentile(blocks, [0.5, 99.5])
    expected = np.rint((np.clip(blocks, low, high) - low) / (high - low) * 255).astype(np.uint8)
    assert np.array_equal(pixels, expected)
    # stored values are high where the image is dark: the top-left corner (high raw) is dark
    assert pixels[0, 0] < pixels[-1, -1]


def test_the_raw_reader_refuses_a_file_of_the_wrong_size(tmp_path, prepare_module):
    path = tmp_path / "JPCLN001.IMG"
    path.write_bytes(b"\0" * 100)
    with pytest.raises(prepare_module.PrepareError, match="exactly 8388608"):
        prepare_module.read_jsrt_raw(path)


def test_the_raw_reader_clips_stray_values_and_refuses_the_wrong_byte_order(
    tmp_path, prepare_module, fake_jsrt
):
    raw = fake_jsrt.raw(0).copy()
    raw[0, :10] = 5000
    path = tmp_path / "JPCLN001.IMG"
    path.write_bytes(raw.tobytes())
    pixels, clipped = prepare_module.read_jsrt_raw(path)
    assert clipped == 10 and pixels.max() == 4095
    path.write_bytes(raw.astype("<u2").tobytes())  # little-endian
    with pytest.raises(prepare_module.PrepareError, match="big-endian"):
        prepare_module.read_jsrt_raw(path)


def test_the_gold_key_round_trips_to_the_observer_1_masks(items, fake_jsrt):
    for item in items:
        key = item["properties"]["gold_key"]
        assert set(key["masks"]) == set(STRUCTURES)
        for name in STRUCTURES:
            mask = key["masks"][name]
            assert mask["size"] == [1024, 1024]
            assert isinstance(mask["counts"], str)  # the compressed COCO form
            assert np.array_equal(rle.decode(mask), fake_jsrt.mask(name, observer=1))
        nodule = "present" if item["properties"]["source_id"].startswith("JPCLN") else "absent"
        assert key["classifications"] == {"nodule": nodule}


def test_every_key_is_a_valid_label_of_the_pilot_ontology(items):
    task = load_ontology(ONTOLOGY)
    for item in items:
        task.validate(item["properties"]["gold_key"])
        task.validate(item["properties"]["observer2_key"])


def test_the_jsrt_data_are_plain_properties(by_source):
    first = by_source["JPCLN001"]["properties"]
    assert first["nodule"] == "present"
    assert first["subtlety"] == 5
    assert first["nodule_size_mm"] == 15
    assert (first["age"], first["sex"]) == (53, "male")
    assert (first["nodule_x"], first["nodule_y"]) == (1634, 692)
    assert first["malignancy"] == "malignant"
    assert first["location"] == "l.upper lobe"
    assert first["diagnosis"] == "lung cancer"
    assert first["source_file"] == "JPCLN001.IMG"
    assert first["source_pixels_clipped"] == 0
    assert "scr_fold" not in first  # the outline files say nothing about folds
    last = by_source["JPCNN002"]["properties"]
    assert (last["nodule"], last["age"], last["sex"]) == ("absent", 41, "female")
    assert "subtlety" not in last


def test_the_cut_columns_of_the_clinical_data_are_put_back_together(fake_jsrt, prepare_module):
    records = prepare_module.parse_metadata(prepare_module.find_metadata(fake_jsrt.jsrt))
    assert sorted(records) == fake_jsrt.ids
    second, third = records["JPCLN002"], records["JPCLN003"]
    assert (second["location"], second["diagnosis"]) == ("r.middle lobe(S4)", "pulmonary hematoma")
    assert second["age"] is None  # '?' in the file
    assert (third["location"], third["diagnosis"]) == ("left lung", "inflammatory mass")
    assert records["JPCNN001"] == {"nodule": "absent", "age": 75, "sex": "male"}


def test_the_items_fit_the_servers_property_limit(items):
    for item in items:
        assert len(json.dumps(item["properties"])) < 256 * 1024


def test_the_observer_report_uses_the_consensus_check(prepared, fake_jsrt):
    report = json.loads((prepared / "observer_report.json").read_text())
    task = load_ontology(ONTOLOGY)
    assert report["images_with_both_observers"] == len(fake_jsrt.ids)
    assert report["consensus_rule"] == task.consensus_rule_version
    for name in STRUCTURES:
        first, second = fake_jsrt.mask(name, observer=1), fake_jsrt.mask(name, observer=2)
        iou = (first & second).sum() / (first | second).sum()
        summary = report["structures"][name]
        assert summary["n"] == len(fake_jsrt.ids)
        assert summary["mean"] == pytest.approx(iou, abs=1e-4)
        threshold = task.ontology.masks[STRUCTURES.index(name)].threshold
        assert summary["threshold"] == threshold
        assert summary["fraction_at_or_above_threshold"] == (1.0 if iou >= threshold else 0.0)
    text = (prepared / "observer_report.txt").read_text()
    assert "left_clavicle" in text and "IoU per structure" in text


def test_a_missing_mask_stops_prepare_before_anything_is_written(
    tmp_path, fake_jsrt, prepare_module, capsys
):
    masks = tmp_path / "masks"
    for path in fake_jsrt.masks1.rglob("*.gif"):
        if not (path.parent.name == "heart" and path.stem == "JPCNN002"):
            target = masks / path.relative_to(fake_jsrt.masks1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    out = tmp_path / "out"
    argv = ["--jsrt", str(fake_jsrt.jsrt), "--scr-masks", str(masks), "--out", str(out)]
    assert prepare_module.main([*argv, "--quiet"]) == 1
    assert "JPCNN002: no mask of heart" in capsys.readouterr().err
    assert not (out / "items.json").exists()


def test_pointing_at_both_observers_at_once_is_refused(tmp_path, fake_jsrt, prepare_module):
    both = tmp_path / "both"
    for source in (fake_jsrt.masks1, fake_jsrt.masks2):
        for path in source.rglob("*.gif"):
            target = both / source.name / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    with pytest.raises(prepare_module.PrepareError, match="one observer's masks only"):
        prepare_module.mask_observer(both)


def test_mask_folders_give_the_same_keys_as_the_outlines(
    tmp_path, fake_jsrt, prepare_module, by_source
):
    out = tmp_path / "out"
    argv = ["--jsrt", str(fake_jsrt.jsrt), "--scr-masks", str(fake_jsrt.masks1)]
    assert prepare_module.main([*argv, "--out", str(out), "--quiet", "--limit", "2"]) == 0
    from_masks = json.loads((out / "items.json").read_text())
    assert len(from_masks) == 2
    from_masks.sort(key=lambda item: item["properties"]["source_id"])
    for mine in from_masks:
        theirs = by_source[mine["properties"]["source_id"]]
        assert mine["properties"]["gold_key"] == theirs["properties"]["gold_key"]
        assert (mine["item"], mine["content_hash"]) == (theirs["item"], theirs["content_hash"])
    assert [m["properties"]["scr_fold"] for m in from_masks] == [1, 2]  # from the folders


def test_an_outline_is_filled_at_pixel_centres(prepare_module):
    # a circle of radius 100 as a 720-gon: the pixels whose centres are inside it
    angles = np.linspace(0, 2 * np.pi, 720, endpoint=False)
    points = list(zip(500 + 100 * np.cos(angles), 400 + 100 * np.sin(angles), strict=True))
    mask = prepare_module.fill_polygon(points)
    rows, cols = np.mgrid[:1024, :1024]
    inside = (cols - 500) ** 2 + (rows - 400) ** 2 < 100**2
    assert (mask ^ inside).sum() <= 8  # only pixels the polygon's chords cut differently
    assert not mask[:299].any() and not mask[501:].any()


def test_an_outline_file_without_a_structure_is_refused(tmp_path, fake_jsrt, prepare_module):
    text = (fake_jsrt.points1 / "JPCLN001.pfs").read_text()
    path = tmp_path / "JPCLN001.pfs"
    path.write_text(text.replace("[Label=heart]", "[Label=hearth]"))
    with pytest.raises(prepare_module.PrepareError, match="no outline of heart"):
        prepare_module.outline_masks(path)


def test_help_runs(prepare_module, capsys):
    with pytest.raises(SystemExit) as stop:
        prepare_module.parse_args(["--help"])
    assert stop.value.code == 0
    assert "--scr-masks2" in capsys.readouterr().out
