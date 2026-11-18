# Man-made change detection from Sentinel-2 (Mumbai and Delhi)

Detects, localises and analyses structural man-made change between two
Sentinel-2 dates and separates it from natural and acquisition-driven change.
CPU only; no GPU required.

| Pair | City | Tile | Before → after | Interval |
|---|---|---|---|---|
| A | Mumbai | T43QBB | 2022-12-23 → 2023-05-12 | 140 days |
| B | Delhi | T43RGM | 2023-10-06 → 2026-10-22 | 1,112 days |

**Read first:** [technical report](docs/TECHNICAL_REPORT.md) ·
[decision log](docs/DECISION_LOG.md) · [AI usage record](AI_USAGE.md).
Phase notes: [data audit](docs/PHASE_1_DATA_AUDIT.md),
[preprocessing](docs/PHASE_2_PREPROCESSING.md),
[architecture](docs/PHASE_3_ARCHITECTURE.md),
[labels and training](docs/PHASE_4_TRAINING.md),
[evaluation and quantization](docs/PHASE_5_EVALUATION.md),
[historical OSM](docs/HISTORICAL_OSM.md).

## Environment

Windows 11 and Python 3.10 (tested); CPU PyTorch wheels.

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m pytest -q
```

Sen2Cor 2.13.02 is needed only to regenerate L2A products (Phase 2).

## Data

The four Sentinel-2A L1C `.SAFE` products (listed with checksums in
[data/manifests/sentinel2_products.csv](data/manifests/sentinel2_products.csv))
are not in git because of their size. Place them in the repository root.
OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0.

## Reproduce

All steps use fixed seeds (20261118) and versioned configs. Run them from the
repository root with `.\venv\Scripts\python.exe -m <module>`.

| Step | Command | Output |
|---|---|---|
| 1. Preprocess and register (Sen2Cor, stacks, masks, registration) | `src.preprocessing.phase2 --help` (see Phase 2 doc) | `outputs/processed/`, `outputs/registered/` |
| 1b. Independent registration check | `src.preprocessing.registration_check` | `outputs/reports/registration_check.{json,md}` |
| 2. Candidate review windows | `src.training.generate_candidates` | `data/labels/candidates/*.geojson` |
| 3. Weak labels | `src.training.weak_labels` | `outputs/weak_labels/` |
| 4. Spatial splits | `src.training.assign_splits` | `data/manifests/spatial_splits.json`, `data/labels/spatial_splits.gpkg` |
| 5. Blind review page | `src.evaluation.make_review_set` → open `outputs/review/index.html`, export to `data/labels/reviews/review_labels.json` | human reference labels |
| 6. Patch bank | `src.training.patches` | `outputs/patches/` |
| 7. Hyperparameter search | `src.training.hparam_search` | `outputs/experiments/hparam_search.csv`, `outputs/models/<run>/` |
| 7b. Final runs and human-corrected fine-tune | `src.training.train --config configs/training/<name>.json` (fine-tune patches: `src.training.reviewed_patches`) | `outputs/models/<run>/` |
| 8. Evaluation | `src.evaluation.evaluate --runs outputs/models/<run> ...` | `outputs/evaluation/` |
| 8b. Window context filter (fit on train-block reviews) | `src.evaluation.window_filter --run outputs/models/ground_ce_final` | `outputs/evaluation/window_filter.json`, `<run>/window_filter.json` |
| 8c. Leave-one-city-out | `src.training.train --config configs/training/ground_ce_loco_train_<city>.json`, then `src.evaluation.loco` | `outputs/evaluation/loco.{json,md}` |
| 9. Quantization | `src.quantization.quantize --runs outputs/models/<run> ...` | `outputs/models/<run>/model_{fp32,int8}.onnx`, `outputs/quantization/` |
| 10. Change maps | `src.inference.predict_tile --model <run or .onnx>` | `outputs/maps/*_change_{class,prob}.tif` |
| 10b. Context grid | `src.inference.context_score --tag <map tag>` | `outputs/maps/*_context.tif` |
| 11. Detection register | `src.inference.register --tag <map tag>` | `outputs/maps/*_detections.gpkg`, `detection_register_*.csv`, `priority_detections_*.gpkg` |
| 12. Evidence gallery | `src.evaluation.evidence --method <method> --tag <map tag>` | `outputs/evidence/` |
| Historical OSM (optional) | `src.utils.historical_osm --city mumbai` | `data/labels/osm/` |
| AI record | `src.utils.export_ai_usage` | `AI_USAGE.md`, `docs/ai_usage/` |

### Quick demonstration

After step 1 and with the committed model artifacts, this runs the ground
model on a 6 × 6 km Delhi region and builds its detection register in a few
seconds (pass a `model_int8.onnx` path to use the quantized model instead):

```powershell
.\venv\Scripts\python.exe -m src.inference.predict_tile --model outputs\models\ground_ce_final --city delhi --region 5516 7004 600 600 --tag demo
.\venv\Scripts\python.exe -m src.inference.register --tag demo --city delhi
```

## Repository layout

```
configs/              versioned rule/config files
data/labels/          candidates, spatial splits, human reviews, OSM (ignored)
data/manifests/       product checksums, split manifest
docs/                 technical report, phase notes, decision log, AI transcripts
src/preprocessing/    Sen2Cor, stacking, masks, registration
src/training/         candidates, weak labels, splits, patches, training, search
src/models/           Siamese U-Net (ground) and MobileNetV3-Small (onboard)
src/evaluation/       review page, evaluation, evidence gallery
src/inference/        sliding-window change maps and detection register
src/quantization/     ONNX export, INT8 quantization and benchmarking
src/utils/            historical OSM downloader, AI usage export
tests/                unit tests (pytest)
```

## Attribution

Sentinel-2 data: Copernicus Sentinel data 2022–2026, ESA. Sen2Cor: ESA.
OpenStreetMap: © OpenStreetMap contributors (ODbL). torchvision
MobileNetV3-Small architecture (BSD-3), trained from scratch with no
pretrained weights. AI assistants were used; see [AI_USAGE.md](AI_USAGE.md).
