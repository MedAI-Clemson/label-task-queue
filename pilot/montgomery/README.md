# Phase 1 pilot: the Montgomery County chest X-ray set

This is the recommended pilot kit. Milestone 8 runs the anchor project on a small real
dataset: one training round and one main workflow. The data are the 138 frontal chest
radiographs of the Montgomery County set: 80 normal and 58 with signs of tuberculosis. The
NLM publishes them with two things the pilot needs:

- a mask of each lung, drawn under a radiologist's supervision;
- each image's TB label.

These give a full `gold_key` for every image. The download needs no registration. The
older JSRT kit ([`pilot/jsrt`](../jsrt/README.md)) is still here as an alternative.

| File | What it is |
| --- | --- |
| `ontology/montgomery_tb.yaml` | The label task `montgomery_tb@1`: a mask of each lung, and a `tb_findings` present/absent answer. The comments explain the thresholds. |
| `workflows/training.yaml` | `montgomery_training_1`: every trainee labels the same 12 gold images, and each label is scored against the key. There is no final label. |
| `workflows/main.yaml` | `montgomery_main`: the other 126 images. Annotators label each once, and 20% get a second read. An expert adjudicates disagreements and blind-reviews a 10% sample. Every label is scored against the key. |
| `prepare.py` | Converts the download into PNGs, gold keys, the import file `items.json` and a report. |
| `setup_pilot.py` | Sets up the project on a running server: roles, accounts, datasets and items. It then starts both workflows. |
| `export_pilot.py` | Downloads everything the audit needs at the end. |
| `accounts.example.csv` | Made-up accounts: 3 trainees, 3 annotators, 2 experts and 1 admin. It holds no passwords. |

`setup_pilot.py` and `export_pilot.py` are thin wrappers. The steps themselves are in
`pilot/common/` and are shared with the JSRT kit.

## Quick start

Run these from the repository root. `DATA` is any folder **outside** the repository.

```bash
pip install -e ".[pilot]"                       # adds Pillow
DATA=$HOME/labelflow-pilot && mkdir -p $DATA

# 1. Data: one zip, 617 MB, no login.
curl -o $DATA/NLM-MontgomeryCXRSet.zip https://openi.nlm.nih.gov/imgs/collections/NLM-MontgomeryCXRSet.zip
sha256sum $DATA/NLM-MontgomeryCXRSet.zip        # 54601e952315d8f67383e9202a6e145997ade429f54f7e0af44b4e158714f424
unzip -q $DATA/NLM-MontgomeryCXRSet.zip -d $DATA -x '__MACOSX/*'

# 2. PNGs, gold keys and items.json (about 2 minutes).
python pilot/montgomery/prepare.py --montgomery $DATA/MontgomerySet --out $DATA/out

# 3. The server: a database, the kit's ontology and the PNG folder.
export DATABASE_URI=sqlite:///$DATA/montgomery_pilot.db
export LABELFLOW_LABEL_TASKS=$PWD/pilot/montgomery/ontology
export LABELFLOW_MEDIA_ROOT=$DATA/out/png
export LABELFLOW_ADMIN_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(18))')
echo "Keep this admin password: $LABELFLOW_ADMIN_PASSWORD"
alembic upgrade head
python -m labelflow.api.admin create-admin root
HOST=127.0.0.1 PORT=8000 ./run.sh &             # serves on http://127.0.0.1:8000/ui/

# 4. Project, roles, the made-up accounts, datasets and items; both workflows start.
python pilot/montgomery/setup_pilot.py --server http://127.0.0.1:8000 --admin root \
    --items $DATA/out/items.json --accounts pilot/montgomery/accounts.example.csv

# 5. At the end (any time, really): the exports, and a check that the log replays.
python pilot/montgomery/export_pilot.py --server http://127.0.0.1:8000 --admin root --out $DATA/exports
python -m labelflow.store.rebuild "$DATABASE_URI" --project montgomery_pilot --verify-only
kill %1                                         # stop the server
```

Step 4 writes each account's generated password to `pilot/montgomery/out/credentials.csv`.
Git ignores that file, and only you can read it. The script prints a warning about it:

- Give each person their own password privately.
- Do not post the passwords in a shared channel.
- Delete the file once everyone has logged in.

### What a dry run on the real data gave (Oct 11, 2026)

The quick start above was run on this machine, with a temporary database and a free port.
The download took 75 s and `prepare.py` took 2 minutes.

- **Prepare.** It wrote 138 PNGs (97 at 841 × 1024, 41 at 1024 × 841; 37 MB). Every gold
  key validated. Shrinking the images moves the lung outlines very little. The mean IoU of
  each original mask against its display-size version is 0.997, and the lowest is 0.995,
  far above the 0.90 threshold.
- **Setup.** It created the project, the roles and the 9 accounts, including the admin. It
  stratified the gold set as 5 TB and 7 normal images. The main set holds 126 images, 53 of
  them TB. Both workflows started:
  - `montgomery_training_1`: 12 images and 36 open slots (3 trainees × 12).
  - `montgomery_main`: 126 images and 151 open `annotate` slots (126, plus 25 second
    reads).
- **Labelers and images.** `trainee_one`, `annotator_one` and `expert_one` each got work
  from `/next`. The UI's image route served each one an 8-bit PNG whose SHA-256 equals the
  item's `content_hash`. The work packets and the labeling page contain no gold key and no
  dataset file name.
- **Simulated labels.** 11 labels were simulated: copies of the gold key, one of them with
  the wrong TB answer. The score reports counted them correctly, 5 items became final, and
  one image was drawn for blind expert review. `export_pilot.py` wrote everything listed in
  step 6 below. The rebuild check replayed the 481 events and verified them (exit 0).

## 1. The data

**Source.** The U.S. National Library of Medicine (NLM, Lister Hill Center) publishes the
"Tuberculosis Chest X-ray Image Data Sets". The data come in two copies:

- one zip: <https://openi.nlm.nih.gov/imgs/collections/NLM-MontgomeryCXRSet.zip>;
- the same files one by one:
  <https://data.lhncbc.nlm.nih.gov/public/Tuberculosis-Chest-X-ray-Datasets/Montgomery-County-CXR-Set/MontgomerySet/>.

The sample files compared were byte for byte the same in both copies. The folder also
holds `montgomery_consensus_roi.csv` (added in 2024), which is not in the zip. It has 109
"TB" bounding boxes on 55 of the TB images, in a downscaled coordinate system that is not
documented. They appear to be the consensus boxes from NLM's later radiologist annotation
work. This kit does not use them, because the label has no box tool.

**Terms**, from the dataset's ReadMe (`NLM-MontgomeryCXRSet-ReadMe.pdf`, in the zip):

- "Please do not share the data set outside of your research group/organization, but
  forward new data set requests to us."
- "Please inform us if you find errors or inconsistencies in the data."
- Publications should attribute the source (National Library of Medicine, National
  Institutes of Health, Bethesda, MD, USA) and cite the two papers below.

The ReadMe names no license. The terms allow research use and hosting on a local server for
the research group. They do not allow putting the images, or the PNGs made from them, on a
public site. The PNGs are derived images, so serve them only to the pilot's participants.

**De-identification.** "The dataset were de-identified by the data providers and were
exempted from IRB review at their institutions. The data set use and public release were
exempted from IRB review (No. 5357) by the NIH Office of Human Research Projections
Programs." Some films still carry burned-in text: the side marker, "PA ERECT" and the
county health department's name. They carry no patient names.

**Cite:**

- Candemir S, Jaeger S, Musco J, Xue Z, Karargyris A, Antani SK, Thoma GR, Palaniappan K.
  Lung segmentation in chest radiographs using anatomical atlases with nonrigid
  registration. *IEEE Trans Med Imaging* 33(2):577–590, 2014. doi:10.1109/TMI.2013.2290491.
- Jaeger S, Karargyris A, Candemir S, Folio L, Siegelman J, Callaghan FM, Xue Z,
  Palaniappan K, Singh RK, Antani SK. Automatic tuberculosis screening using chest
  radiographs. *IEEE Trans Med Imaging* 33(2):233–245, 2014. doi:10.1109/TMI.2013.2284099.
- The dataset paper: Jaeger S, Candemir S, Antani S, Wáng YX, Lu PX, Thoma G. Two public
  chest X-ray datasets for computer-aided screening of pulmonary diseases. *Quant Imaging
  Med Surg* 4(6):475–477, 2014.

**What is in it, and its quirks:**

- `CXR_png/MCUCXR_<4 digits>_<0|1>.png`: 138 images. The last digit is the label: 0 is
  normal, 1 is a manifestation of TB. The ReadMe says the images are 12-bit, but the files
  are **8-bit** greyscale PNGs, already in display polarity (bone bright).
  - 97 are 4020 × 4892 (portrait) and 41 are 4892 × 4020 (landscape).
  - The pixel spacing is 0.0875 mm.
- `ManualMask/leftMask/` and `ManualMask/rightMask/`: one 1-bit PNG per image, at the
  image's size.
  - **The folders are named by image side, not patient side.** `leftMask` holds the lung
    on the left of the image, which is the patient's **right** lung. This was checked on
    all 138 images: the `leftMask` lung always lies left of the `rightMask` one, and on the
    images inspected the "R" marker is on the image's left. `prepare.py` swaps the two, so the label task's
    `right_lung` is the patient's right, and it checks every image.
  - The masks never overlap.
  - Mask convention, from the ReadMe: posterior and anterior ribs visible; "the part of
    the lung behind the heart is excluded"; boundaries follow the heart, aortic arc and
    pericardium lines and a sharp costophrenic angle; "an inferred boundary when the
    pathology is severe".
- `ClinicalReadings/MCUCXR_*.txt`: `Patient's Sex: F|M|O`, `Patient's Age: 031Y` and a
  one-line reading.
  - 5 files have CRLF line ends.
  - One has sex `O`.
  - Ages run from 4 to 89, so children are included.
  - Three readings name other images of the same patient: 0113/0117, and 0162/0166/0170.
- **The file names give the answer away**, through the last digit and through the numbers
  (every normal image is numbered 0001–0103, every TB image 0104 and up). The labeling page
  shows an item's name. So `prepare.py` names each item `mc_` plus 10 hex digits of the
  source PNG's SHA-256, keeps the original name only as the hidden property `source_id`,
  and lists the items in the order of the new names.
- **"TB" includes old disease.** Of the 58 TB images, 24 readings describe inactive,
  healed, calcified or stable findings, or have no report (`MCUCXR_0166_1`: "NO REPORT").
  The dataset still counts them as TB manifestations, so their gold answer is `present`.
  `report.txt` lists them.
- The zip also holds `__MACOSX/`, `.DS_Store` and `Thumbs.db` files. `prepare.py` ignores
  them.

## 2. Prepare

```bash
python pilot/montgomery/prepare.py --montgomery $DATA/MontgomerySet --out $DATA/out
```

`--help` lists the options (`--side`, `--media-root`, `--limit`). It writes:

- `out/png/mc_*.png`: 8-bit, with the longest side 1024 pixels.
  - Shrinking averages over areas (a box filter).
  - The masks are shrunk the same way, and a pixel is in a mask when at least half of its
    area was. So the masks stay on the image's grid.
- `out/items.json`: one entry per image with its `file://` uri, the PNG's SHA-256 and these
  properties:
  - `gold_key`: the two masks as COCO RLE, plus the TB answer, validated against
    `montgomery_tb@1`;
  - `tb_findings`, used to stratify the gold set;
  - from the reading: `sex`, `age`, `reading` and `same_patient_as` (as item names);
  - `source_id`, `source_file`, `source_sha256` and `source_size`.
- `out/report.txt` and `.json`:
  - counts and sizes;
  - lung areas;
  - how far shrinking moved each mask (resize IoU);
  - the TB images whose reading describes old or inactive disease.

If the server will read the PNGs from another folder, move them there and pass that folder
as `--media-root`, so the uris point at it.

## 3. Run the server

As in the quick start, with these settings:

- `LABELFLOW_LABEL_TASKS` is the kit's ontology folder. To serve other ontologies as well,
  copy `montgomery_tb.yaml` into their folder.
- `LABELFLOW_MEDIA_ROOT` is the PNG folder.

`./run.sh` brings the database up to date and serves with auto-reload. For a long-running
pilot, run `uvicorn labelflow.api:app --host … --port …` without `--reload`.

## 4. Set up the pilot

`accounts.example.csv` lists made-up people. `roles` is `;`-separated and holds the project
roles `trainee`, `annotator` and `expert`. The role `admin` makes a server administrator
(here `pilot_admin`, for whoever runs the pilot day to day). Replace the rows with the real
participants when they are known.

The file has no password column, and no passwords belong in the repository. A `password`
column is still read if present, for passwords set elsewhere. Empty passwords, and all of
them when the column is absent, are generated at setup.

```bash
python pilot/montgomery/setup_pilot.py --server http://127.0.0.1:8000 --admin root \
    --items $DATA/out/items.json --accounts pilot/montgomery/accounts.example.csv
```

The script does the following:

- It creates the project `montgomery_pilot`, the three roles and the accounts.
  Generated passwords are appended to `pilot/montgomery/out/credentials.csv`
  (`--credentials` to change where).
- It grants the roles, giving a reason for each.
- It splits the images with `--seed` (default 20261011): 12 go to `montgomery_gold`, 5
  with TB findings and 7 without, and the other 126 go to `montgomery_main`. All images of
  one patient stay on the same side; `prepare.py`'s `same_patient_as` property says which
  images those are. On the real data, both patients with several images (three images in
  all for one, two for the other) land in the main set.
- It imports the images in batches and starts both workflows with the same seed.
- It prints the validator's warnings and the next steps.

Running it again only does what is missing. It does not start the training round while
nobody holds `trainee`.

Staffing: `montgomery_main` needs two experts and two annotators. The reasons are the same as in
  the JSRT kit: a disputed double read that is also drawn for review needs two different
  experts, and a double read needs two annotators. The example accounts have both.

## 5. What to label (for participants)

Give this section to everyone who labels. Log in at `http://<server>/ui/`, pick your
workflow and press "Get work". Each image asks for three things.

**Left and right are the patient's.** The patient's right lung is on the **left** of the
image, on the side of the "R" marker.

1. **Right lung**, then **left lung**: one mask each. Follow the dataset's convention:
   - leave out the part of the lung behind the heart;
   - follow the heart border, and the diaphragm down into a sharp costophrenic angle;
   - where disease hides the edge, draw the boundary you infer.
2. **TB findings: present or absent.** Answer **present** if the film shows **any
   radiographic sign of TB, active or old**. Active signs are infiltrates, cavities,
   effusion and miliary nodules. Old signs are scars, fibrosis, volume loss and calcified
   granulomas. Answer **absent** only for a normal film. Do not try to judge whether the
   disease is active: that is not the question.

What each role does:

- **Trainees**, in `montgomery_training_1`: label each of the 12 training images. The
  answer key is never shown to you. Afterwards the admin goes through your score report
  with you.
- **Annotators**, in `montgomery_main`: label images until none are left. About one image
  in five is also labeled by a second annotator. Neither of you sees the other's label.
- **Experts**, in `montgomery_main`, have two kinds of work:
  - **Adjudication:** two annotators disagreed. You see both labels as layers, with the
    disagreements flagged, and you submit a complete label. "Start from this layer" copies
    one label into the editor for you to correct.
  - **Blind review** of a 10% sample: you label the image without seeing the annotation.
- **Admin**: when a trainee is ready, grants them `annotator` and revokes `trainee`, with a
  reason, on the project page.

## 6. Export, and the audit

```bash
python pilot/montgomery/export_pilot.py --server http://127.0.0.1:8000 --admin root --out $DATA/exports
python -m labelflow.store.rebuild "$DATABASE_URI" --project montgomery_pilot --verify-only
```

`exports/` then holds:

- `events.jsonl` and `roles.json`;
- per workflow:
  - `items.csv`/`.parquet` and `labels.csv`/`.parquet`;
  - `scores.json`, `sampling.json` and `versions.json`;
  - `explain/<item>.txt`;
  - `structure_scores.csv`: per stage, labeler and field (`right_lung`, `left_lung`,
    `tb_findings`), the labels scored and agreed, with the mean and lowest IoU.

The JSRT kit's section 7 table shows where each audit question is answered; it holds for
this kit unchanged.

Beyond the checklist, the pilot measures:

- each person's quality against the NLM masks and TB labels;
- annotator agreement on the double reads;
- anchoring in adjudication (`started_from`, `changed_from_start`).

It also shows whether the reading-based TB answer is one readers can reproduce from the
film. Join `labels.csv` to the `reading` property to see the inactive-disease cases.

## Decisions (Hudson, Oct 11, 2026)

- **`tb_findings` means any radiographic sign of TB, active or old.** This matches the
  dataset's label, which counts old disease as a TB manifestation: 24 of the 58 TB images
  have readings of inactive, healed or stable disease. Participants are told so in
  section 5.
- **The lung threshold stays at 0.90 for the pilot.** I found no published inter-observer
  study of the Montgomery masks, and the dataset has a single observer. So 0.90 rests on
  the SCR study's human-observer agreement on JSRT lungs (IoU 0.946 ± 0.018); see the
  ontology.
  - After the pilot it will be revisited from the agreement of our own double reads: the
    lung IoUs in the `consensus_checked` events of `montgomery_main`.
  - A new threshold means a new label-task version (`montgomery_tb@2` in a new file),
    because workflows and recorded labels name the version they used.
- **The gold set has 12 images.**
- **Each patient's images stay together** in the split; see section 4.

The dataset also includes one sex `O` and children as young as 4. Both are kept as given.
