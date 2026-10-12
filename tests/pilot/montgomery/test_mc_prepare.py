"""The Montgomery kit's prepare.py, end to end on synthetic data in the real formats."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from labelflow.labeltask import load_ontology, rle

Image = pytest.importorskip(
    "PIL.Image", reason="the pilot kit needs Pillow: pip install -e '.[pilot]'"
)

ROOT = Path(__file__).resolve().parents[3]
ONTOLOGY = ROOT / "pilot" / "montgomery" / "ontology" / "montgomery_tb.yaml"
LUNGS = ["right_lung", "left_lung"]


@pytest.fixture(scope="module")
def items(mc_prepared: Path) -> list[dict]:
    return json.loads((mc_prepared / "items.json").read_text())


@pytest.fixture(scope="module")
def by_source(items: list[dict]) -> dict[str, dict]:
    return {item["properties"]["source_id"]: item for item in items}


def display_shape(size: tuple[int, int]) -> tuple[int, int]:
    """(rows, columns) of the PNG for a source of (width, height)."""
    return (1024, 841) if size[1] > size[0] else (841, 1024)


def test_every_radiograph_becomes_an_item_with_its_png_and_hash(
    mc_prepared, items, by_source, fake_montgomery
):
    assert sorted(by_source) == fake_montgomery.ids  # the Mac leftovers are ignored
    for source_id, item in by_source.items():
        png = mc_prepared / "png" / f"{item['item']}.png"
        assert item["uri"] == png.resolve().as_uri()
        assert item["content_hash"] == hashlib.sha256(png.read_bytes()).hexdigest()
        with Image.open(png) as image:
            assert (image.format, image.mode) == ("PNG", "L")
            assert image.size[::-1] == display_shape(fake_montgomery.size(source_id))


def test_item_names_do_not_give_the_answer_away(items, by_source, fake_montgomery):
    for source_id, item in by_source.items():
        source = fake_montgomery.root / "CXR_png" / f"{source_id}.png"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert item["item"] == f"mc_{digest[:10]}"
        assert item["properties"]["source_sha256"] == digest
        assert "MCUCXR" not in item["item"] and "MCUCXR" not in item["uri"]
    # listed in the order of the opaque names, not the dataset's (normal images first)
    assert [i["item"] for i in items] == sorted(i["item"] for i in items)


def test_the_dataset_mask_folders_are_by_image_side(by_source, fake_montgomery):
    # leftMask (the lung on the image's left) is the patient's right lung
    for source_id, item in by_source.items():
        masks = {name: rle.decode(item["properties"]["gold_key"]["masks"][name]) for name in LUNGS}
        right_columns = np.nonzero(masks["right_lung"])[1]
        left_columns = np.nonzero(masks["left_lung"])[1]
        assert right_columns.max() < left_columns.min(), source_id


def test_the_masks_are_shrunk_with_the_image_and_stay_aligned(
    mc_prepared, by_source, fake_montgomery
):
    for source_id, item in by_source.items():
        with Image.open(mc_prepared / "png" / f"{item['item']}.png") as image:
            pixels = np.asarray(image)
        key = item["properties"]["gold_key"]["masks"]
        lungs = rle.decode(key["right_lung"]) | rle.decode(key["left_lung"])
        assert lungs.shape == pixels.shape == display_shape(fake_montgomery.size(source_id))
        # The image is 220 inside the lungs and 40 outside, so area averaging gives 130 or
        # more exactly where at least half a pixel was lung: where the mask is (up to the
        # rounding of pixels covered almost exactly half).
        assert np.logical_xor(pixels >= 130, lungs).sum() <= 10
        # and each mask is the original rectangle scaled by 1024 / 4892
        for lung in LUNGS:
            original = fake_montgomery.mask(source_id, lung)
            scale = 1024 / max(original.shape)
            rows, columns = np.nonzero(original)
            shrunk = rle.decode(key[lung])
            srows, scolumns = np.nonzero(shrunk)
            for full, small in ((rows, srows), (columns, scolumns)):
                assert abs(small.min() - full.min() * scale) <= 1
                assert abs(small.max() + 1 - (full.max() + 1) * scale) <= 1


def test_every_key_is_a_valid_label_of_the_ontology(items):
    task = load_ontology(ONTOLOGY)
    for item in items:
        task.validate(item["properties"]["gold_key"])


def test_the_tb_answer_comes_from_the_file_name(by_source):
    for source_id, item in by_source.items():
        answer = {"0": "absent", "1": "present"}[source_id[-1]]
        assert item["properties"]["gold_key"]["classifications"] == {"tb_findings": answer}
        assert item["properties"]["tb_findings"] == answer


def test_the_clinical_readings_are_plain_properties(by_source):
    first = by_source["MCUCXR_0001_0"]["properties"]
    assert (first["sex"], first["age"], first["reading"]) == ("F", 27, "normal")
    assert first["source_size"] == [4020, 4892]
    assert by_source["MCUCXR_0002_0"]["properties"]["sex"] == "O"  # as the dataset gives it
    crlf = by_source["MCUCXR_0117_1"]["properties"]
    assert crlf["age"] == 85
    assert crlf["reading"].startswith("stable CXR since 6M") and "\r" not in crlf["reading"]
    # the other image of the same patient, by its item name
    assert crlf["same_patient_as"] == [by_source["MCUCXR_0104_1"]["item"]]
    for item in by_source.values():
        assert len(json.dumps(item["properties"])) < 256 * 1024  # the server's limit


def test_the_report(mc_prepared, by_source):
    report = json.loads((mc_prepared / "report.json").read_text())
    assert report["images"] == 4
    assert report["tb_findings"] == {"absent": 2, "present": 2}
    assert report["display_sizes"] == {"1024 x 841": 2, "841 x 1024": 2}
    for lung in LUNGS:
        assert report["resize_iou"][lung]["min"] > 0.99
    noted = {row["source_id"] for row in report["tb_readings_old_inactive_or_missing"]}
    assert noted == {"MCUCXR_0104_1", "MCUCXR_0117_1"}
    text = (mc_prepared / "report.txt").read_text()
    assert "resize IoU" in text and by_source["MCUCXR_0104_1"]["item"] in text


def copy_set(fake_montgomery, tmp_path: Path) -> Path:
    root = tmp_path / "MontgomerySet"
    shutil.copytree(fake_montgomery.root, root)
    return root


def test_swapped_mask_folders_are_refused(fake_montgomery, mc_prepare, tmp_path, capsys):
    root = copy_set(fake_montgomery, tmp_path)
    masks = root / "ManualMask"
    (masks / "leftMask").rename(masks / "swap")
    (masks / "rightMask").rename(masks / "leftMask")
    (masks / "swap").rename(masks / "rightMask")
    out = tmp_path / "out"
    assert mc_prepare.main(["--montgomery", str(root), "--out", str(out), "--quiet"]) == 1
    assert "patient's right lung (leftMask) is not on the left" in capsys.readouterr().err
    assert not (out / "items.json").exists()


def test_a_missing_mask_stops_prepare_before_anything_is_written(
    fake_montgomery, mc_prepare, tmp_path, capsys
):
    root = copy_set(fake_montgomery, tmp_path)
    (root / "ManualMask" / "rightMask" / "MCUCXR_0104_1.png").unlink()
    out = tmp_path / "out"
    assert mc_prepare.main(["--montgomery", str(root), "--out", str(out), "--quiet"]) == 1
    assert "MCUCXR_0104_1: no left_lung mask" in capsys.readouterr().err
    assert not out.exists()


def test_a_mask_of_the_wrong_size_is_refused(fake_montgomery, mc_prepare, tmp_path):
    root = copy_set(fake_montgomery, tmp_path)
    Image.fromarray(np.ones((100, 80), dtype=bool)).save(
        root / "ManualMask" / "leftMask" / "MCUCXR_0001_0.png"
    )
    with pytest.raises(mc_prepare.PrepareError, match="is 80 x 100, but its image is 4020 x 4892"):
        mc_prepare.prepare(
            mc_prepare.parse_args(["--montgomery", str(root), "--out", str(tmp_path / "o")])
        )


def test_two_copies_of_the_dataset_are_refused(fake_montgomery, mc_prepare, tmp_path):
    shutil.copytree(fake_montgomery.root, tmp_path / "a" / "MontgomerySet")
    shutil.copytree(fake_montgomery.root, tmp_path / "b" / "MontgomerySet")
    with pytest.raises(mc_prepare.PrepareError, match="one copy of the dataset"):
        mc_prepare.find_sources(tmp_path)


def test_a_reading_without_an_age_is_refused(tmp_path, mc_prepare):
    path = tmp_path / "MCUCXR_0001_0.txt"
    path.write_text("Patient's Sex: F \nPatient's Age: ?\nnormal\n")
    with pytest.raises(mc_prepare.PrepareError, match="cannot read the age"):
        mc_prepare.parse_reading(path)


def test_display_sizes(mc_prepare):
    assert mc_prepare.display_size(4020, 4892) == (841, 1024)
    assert mc_prepare.display_size(4892, 4020) == (1024, 841)
    assert mc_prepare.display_size(4020, 4892, 512) == (421, 512)
    task = load_ontology(ONTOLOGY)
    assert task.ontology.max_mask_pixels == 841 * 1024


def test_help_runs(mc_prepare, capsys):
    with pytest.raises(SystemExit) as stop:
        mc_prepare.parse_args(["--help"])
    assert stop.value.code == 0
    assert "--montgomery" in capsys.readouterr().out
