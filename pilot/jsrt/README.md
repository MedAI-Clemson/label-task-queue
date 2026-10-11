# Phase 1 pilot: JSRT chest X-rays with the SCR segmentations

Milestone 8 runs the anchor project on a small real dataset: one training round and one
main workflow. The data are the 247 JSRT radiographs. The gold standard is the SCR
reference segmentation of five structures: both lungs, the heart and both clavicles.

| File | What it is |
| --- | --- |
| `ontology/jsrt_scr.yaml` | The label task `jsrt_scr@1`: five masks and a nodule yes/no. The comments explain each threshold. |
| `workflows/training.yaml` | `jsrt_training_1`: every trainee labels the 20 gold images. Each label is scored against the key. There is no final label. |
| `workflows/main.yaml` | `jsrt_main`: the other 227 images. Annotators label each once, and 20% get a second read. An expert adjudicates disagreements and blind-reviews a 10% sample. Every label is scored against the key. |
| `prepare.py` | Converts the downloads into PNGs, gold keys and the import file `items.json`. |
| `setup_pilot.py` | Sets up the project, roles, accounts, datasets and items on a running server, and starts both workflows. |
| `export_pilot.py` | Downloads everything the audit needs at the end. |

## 1. Get the data (once, by hand)

Keep the data and every output out of the repository. `.gitignore` covers `data/` and
`out/` here.

**JSRT images.** Download from <http://db.jsrt.or.jp/eng.php>.

- Register at <http://db.jsrt.or.jp/eng-01.php>. The password is emailed to you, and it
  stops working after 10 logins, after which you register again.
- The terms: the database is free of charge but *not in the public domain*. "Please do not
  distribute any of the images … and do not use the JSRT database for any commercial
  purposes."
- Cite: Shiraishi J, Katsuragawa S, Ikezoe J, et al. Development of a digital image database
  for chest radiographs with and without a lung nodule. *AJR* 174:71–74, 2000.
- You need all 247 `.IMG` files and the clinical data files `CLNDAT_EN.txt` and
  `CNNDAT_EN.TXT`.
- The old address `www.jsrt.or.jp/jsrt-db/`, which some pages still link to, is dead.

**SCR segmentations.** Download `points.zip` from the SCR record on Zenodo,
<https://zenodo.org/record/7056076> (doi:10.5281/zenodo.7056076).

- Zenodo lists the record under CC BY 4.0.
- The original SCR site (now offline, [archived](http://web.archive.org/web/2012/http://www.isi.uu.nl:80/Research/Databases/SCR/index.php))
  said: "for research and educational purposes. Redistribution and commercial use is
  prohibited". It asked users to cite both papers.
- Cite: van Ginneken B, Stegmann MB, Loog M. Segmentation of anatomical structures in chest
  radiographs using supervised methods: a comparative study on a public database. *Medical
  Image Analysis* 10(1):19–40, 2006.
- `points/*.pfs` holds the first observer's clicked outlines of all five structures for all
  247 images, on the 1024 × 1024 grid. `prepare.py` fills them. On the right lung this
  reproduces the published masks (IoU ≥ 0.998 on every image).
- Do not use the record's `masks.zip`: it holds the right lung only. `jpg.zip` is not
  needed.
- If you have the original SCR download, its GIF masks in `fold1/masks/left lung/…`
  folders work too: use `--scr-masks` instead of `--scr-points`.
- The **second observer's** segmentations were made for all 247 images but are not in any
  public download. Ask the authors for them if you want the observer report described in
  step 2.

The PNGs that `prepare.py` writes are derived JSRT images, so treat them like the
originals: serve them only from a private server, to the pilot's participants.

## 2. Prepare

```bash
pip install -e ".[pilot]"     # adds Pillow
python pilot/jsrt/prepare.py --jsrt data/jsrt --scr-points data/scr/points --out out/
```

This takes about two minutes. `--help` lists the options. It writes:

- `out/png/JPC*.png`: 1024 × 1024, 8-bit.
  - The 2048 × 2048 12-bit image is averaged over 2 × 2 blocks, so it lands on the
    masks' grid.
  - It is then inverted, because the stored values are high where the film is dark.
  - Finally it is windowed between the image's own 0.5th and 99.5th percentiles.
  - `--window full` maps the whole 12-bit range instead.
- `out/items.json`: one entry per image with its `file://` uri, the PNG's SHA-256 and these
  properties:
  - `gold_key`: the SCR masks as COCO RLE, plus the nodule answer;
  - `observer2_key`, if given;
  - the JSRT data: `nodule`, `subtlety`, `nodule_size_mm`, `nodule_x`, `nodule_y`,
    `malignancy`, `location`, `diagnosis`, `age`, `sex`;
  - the source file and its hash.

  Every key is validated against `jsrt_scr@1` first.
- `out/observer_report.txt`: with `--scr-points2` or `--scr-masks2`, the IoU between the
  observers per structure, computed by the label task's own consensus check, with how
  often each threshold is met.

If the server will read the PNGs from another folder, move them there and pass that folder
as `--media-root`, so the uris point at it.

## 3. Run the server

```bash
export DATABASE_URI=sqlite:///$PWD/db/jsrt_pilot.db
export LABELFLOW_LABEL_TASKS=$PWD/pilot/jsrt/ontology   # a folder; it holds jsrt_scr.yaml
export LABELFLOW_MEDIA_ROOT=$PWD/out/png
./run.sh                                                # migrates, then serves on :8000
python -m labelflow.api.admin create-admin root         # once, in another terminal
```

To serve other ontologies as well, copy `jsrt_scr.yaml` into their folder.

## 4. Set up the pilot

Write a CSV of the people taking part. `roles` is `;`-separated; leave `password` empty to
have one generated.

```csv
username,display_name,roles,password
tara,Tara,trainee,
ana,Ana,annotator,
dr_smith,Dr Smith,expert,
```

```bash
python pilot/jsrt/setup_pilot.py --server http://localhost:8000 --admin root \
    --items out/items.json --accounts people.csv
```

The script then does the following:

- It creates the project `jsrt_pilot`, the roles `trainee`, `annotator` and `expert`, and
  the accounts. Generated passwords go to `pilot_credentials.csv`, readable only by you.
- It grants the roles, giving a reason for each.
- It splits the images with `--seed` (default 20261011): 20 go to `jsrt_gold`, 12 with a
  nodule and 8 without, and the other 227 go to `jsrt_main`.
- It imports the images in batches and starts both workflows with the same seed.
- It prints the validator's warnings and the next steps.

Running it again only does what is missing.

It does not start the training round while nobody holds `trainee`. That round gives every
image to the trainees who hold the role *when it starts*, so grant the role first.

Two staffing notes:

- `jsrt_main` needs **two experts**. A disputed double read that is also drawn for review
  (about 1–2% of images) needs two different experts, and the validator warns if only one
  person holds `expert`. With one expert, delete the `expert_review` stage from
  `main.yaml` before starting.
- `annotate` needs two annotators for its double reads.

## 5. What each person does

Everyone logs in at `http://<server>/ui/`, picks a workflow and presses "Get work".
**Left and right are the patient's**: the right lung is on the left of the image.

- **Trainees**, in `jsrt_training_1`: draw all five structures and answer "nodule" on each
  of the 20 gold images. The key is never shown to them. Afterwards the admin reviews the
  score report with them, outside the tool.
- **Admin**: when a trainee is ready, grants them `annotator` and revokes `trainee`, with a
  reason. Use the project page or `POST /projects/jsrt_pilot/roles/annotator/grant`.
- **Annotators**, in `jsrt_main`: label images until none are left. About one image in five
  is also read by a second annotator. Neither sees the other's label.
- **Experts**, in `jsrt_main`, have two kinds of work:
  - Adjudication: the two reads are shown as layers, with per-structure flags. The expert
    submits a complete label.
  - Blind review: the expert labels the image without seeing the annotation.

  The interface cannot start from an earlier layer, so every expert label is drawn in full.

## 6. Export at the end

```bash
python pilot/jsrt/export_pilot.py --server http://localhost:8000 --admin root --out exports/
python -m labelflow.store.rebuild "$DATABASE_URI" --project jsrt_pilot --verify-only
```

`exports/` then holds:

- `events.jsonl`: the whole event log, with its hash chain;
- `roles.json`;
- for each workflow:
  - `items.csv` and `labels.csv`, also as Parquet;
  - `scores.json` and `sampling.json`;
  - `versions.json`: the config versions with their diffs;
  - `explain/<item>.txt` for every image;
  - `structure_scores.csv`: per stage, labeler and structure, how many labels met the
    threshold against `gold_key`, with the mean and lowest IoU, taken from the
    `label_scored` events.

## 7. The audit checklist, answered from the exports

| Question (design notes, milestone 8) | Where the answer is |
| --- | --- |
| For any final label: which labels produced it, who made them, their roles then, the stage, the config version, the rule | `items.csv` gives `final_label`, `source_stage`, `label_ids`, `rule` and `config_version`. Join `label_ids` to `labels.csv` for `labeler`, `roles_at_claim`, `roles`, `stage` and `config_version`. The same trail is in `explain/<item>.txt`. |
| For any sampling decision: the draw, the threshold, the version | The `admission_decided` events in `events.jsonl` have the draw and the threshold, under the config version. `explain/<item>.txt` prints them on the admission lines. `sampling.json` gives the rates actually drawn, overall and per annotator. Anyone can recompute a draw from the seed: `labelflow.engine.draw(seed, stage, item)`. |
| For any label: what the labeler was shown | `labels.csv` has `shown_content_hash`, `shown_prior_ids`, `show_prior` and `shown_consensus_rule`. Check `shown_content_hash` against the item's `content_hash` and against `sha256sum out/png/<item>.png`. |
| For any item still waiting: why | `items.csv` rows with `status` = `in_progress`, then the "waiting" part of `explain/<item>.txt`. |
| For any role change or config edit: who, when, what | The `role_granted`, `role_revoked` and `config_activated` events in `events.jsonl` (`actor`, `at`, `because` or reason). `versions.json` has each edit's diff. |
| Rebuilding from the log reproduces the current state | `python -m labelflow.store.rebuild … --verify-only` exits 0. It replays the log, compares the result with the tables and verifies the hash chain. |

Beyond the checklist, the pilot measures:

- **Quality against the gold standard**, from `scores.json` and `structure_scores.csv`:
  each trainee, annotator and expert against the SCR reference, per structure.
- **Annotator agreement** on the double reads, from the `consensus_checked` events and the
  `.disagreed` count.
- **Nodule detection by difficulty**: join `labels.csv` to the `subtlety` property in
  `items.json`.
