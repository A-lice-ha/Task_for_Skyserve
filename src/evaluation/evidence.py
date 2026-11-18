"""Before/after evidence panels for accepted, rejected and failed cases.

Selection uses the human review and one method's window decisions
(outputs/evaluation/window_predictions.csv); held-out (test/zero-shot) windows
are preferred. Each panel: T1 | T2 | T2 + model change outline (red gain,
cyan loss). Writes outputs/evidence/<category>_<n>_<feature_id>.jpg and
outputs/evidence/README.md.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window

from src.evaluation.evaluate import NATURAL, POSITIVE, REVIEWS
from src.evaluation.windows import load_reviews

ROOT = Path(__file__).resolve().parents[2]
CHIPS = ROOT / "outputs" / "review" / "chips"
MAPS = ROOT / "outputs" / "maps"
OUT = ROOT / "outputs" / "evidence"
PER_CATEGORY = 4
SCALE = 2


def _overlay(img: np.ndarray, classes: np.ndarray) -> np.ndarray:
    out = img.copy()
    big = cv2.resize(classes, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_NEAREST)
    for code, colour in ((1, (40, 40, 255)), (2, (255, 230, 0))):
        contours, _ = cv2.findContours((big == code).astype(np.uint8), cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(out, contours, -1, colour, 1)
    return out


def _label(img: np.ndarray, text: str) -> np.ndarray:
    bar = np.full((22, img.shape[1], 3), 24, np.uint8)
    cv2.putText(bar, text, (6, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (235, 235, 235), 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def _window_classes(city: str, tag: str, fid: str) -> np.ndarray:
    row, col = map(int, re.search(r"R(\d+)_C(\d+)", fid).groups())
    with rasterio.open(MAPS / f"{city}_{tag}_change_class.tif") as src:
        data = src.read(1, window=Window(col, row, 256, 256), boundless=True, fill_value=255)
    data[data == 255] = 0
    return data


def select(preds: pd.DataFrame) -> dict[str, pd.DataFrame]:
    held = preds["split"].isin(["test", "zero_shot"])
    order = lambda d: d.assign(_h=~held.loc[d.index]).sort_values(["_h", "confidence"], ascending=[True, True])
    pos = preds["human_class"].isin(POSITIVE)
    nat = preds["human_class"].isin(NATURAL)
    return {
        "accepted": order(preds[pos & preds["flagged"]]),
        "rejected_natural": order(preds[nat & ~preds["flagged"]]),
        "failure_false_alarm": order(preds[(nat | (preds["human_class"] == "no_change")) & preds["flagged"]]),
        "failure_missed": order(preds[pos & ~preds["flagged"]]),
        "uncertain": order(preds[preds["human_class"] == "uncertain"]),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--method", required=True, help="method name in window_predictions.csv")
    parser.add_argument("--tag", required=True, help="change-map tag from predict_tile.py")
    args = parser.parse_args(argv)
    preds = pd.read_csv(ROOT / "outputs" / "evaluation" / "window_predictions.csv")
    preds = preds[preds["method"] == args.method].copy()
    notes = {r["feature_id"]: r.get("notes", "") for r in load_reviews(REVIEWS)}
    preds["confidence"] = preds["confidence"].map({"high": 0, "medium": 1, "low": 2}).fillna(3)
    OUT.mkdir(parents=True, exist_ok=True)
    for old in [*OUT.glob("*.png"), *OUT.glob("*.jpg")]:
        old.unlink()
    lines = [f"# Evidence gallery\n\nMethod: `{args.method}` · change map: `{args.tag}`. "
             "Panels: T1 | T2 | T2 + model outline (red gain, cyan loss). "
             "True colour B04/B03/B02, shared stretch per window; 10 m pixels, 2.56 km windows. "
             "Human labels come from the blind two-date review.\n"]
    for category, frame in select(preds).items():
        lines.append(f"\n## {category.replace('_', ' ').capitalize()} ({len(frame)} windows)\n")
        for n, (_, r) in enumerate(frame.head(PER_CATEGORY).iterrows(), start=1):
            fid, city = r["feature_id"], r["city"].lower()
            t1 = cv2.imread(str(CHIPS / f"{fid}_t1.jpg"))
            t2 = cv2.imread(str(CHIPS / f"{fid}_t2.jpg"))
            ov = _overlay(t2, _window_classes(city, args.tag, fid))
            panel = np.hstack([_label(t1, "T1"), _label(t2, "T2"), _label(ov, "T2 + model")])
            name = f"{category}_{n}_{fid}.jpg"
            cv2.imwrite(str(OUT / name), panel, [cv2.IMWRITE_JPEG_QUALITY, 92])
            note = str(notes.get(fid) or "").replace("\n", " ")
            lines.append(f"- **{fid}** ({r['city']}, {r['split']}): human `{r['human_class']}`; "
                         f"model gain {r['gain_px']} px / loss {r['loss_px']} px, flagged={r['flagged']}. "
                         f"{('Reviewer note: ' + note) if note else ''}\n\n  ![{fid}]({name})\n")
    (OUT / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote evidence to {OUT}")


if __name__ == "__main__":
    main()
