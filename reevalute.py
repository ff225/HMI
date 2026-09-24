"""
Re-evaluate a completed campaign offline, without calling any model.

Generation and verification are decoupled: every stylesheet produced during the
campaign is stored under artifacts/, so a change to the enforcement criterion is
re-applied by replaying the saved configurations through the checker. Inference
columns (gen_seconds, output_chars, parsed) are carried over unchanged; only the
verdict is recomputed.

Both criteria are written for every run, so the two can be reported side by side:

  mandate  a rule that exceeds the machine's declared adaptable surface is a
           violation whatever it renders (the original criterion)
  outcome  authority codes are recorded but do not block; only the measured
           render decides, plus output that cannot be used at all

The input results.csv is never modified.

Usage:
    python reevaluate.py --out results_reevaluated.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib

from playwright.sync_api import sync_playwright

from checker import baseline_reference, enforce, load_manifest

HERE = pathlib.Path(__file__).parent

CARRY = ["profile_id", "k", "machine_id", "arm", "model", "rep",
         "gen_seconds", "output_chars", "parsed",
         "font_scale", "target_min", "panels_hidden"]

FIELDS = CARRY + [
    "committed_mandate", "safety_mandate",
    "committed_outcome", "safety_outcome",
    "integrity", "authority_codes", "outcome_codes",
]

AUTHORITY = {"forbidden_property", "protected_selector", "range_violation"}
UNUSABLE = {"parse_error", "schema_error"}


def stem_of(r: dict) -> str:
    return (f"{r['model'].replace(':', '-')}_{r['machine_id']}_"
            f"{r['profile_id']}_{r['arm']}_r{r['rep']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results.csv")
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--out", default="results_reevaluated.csv")
    args = ap.parse_args()

    src = HERE / args.results
    art = HERE / args.artifacts
    dst = HERE / args.out
    if dst.resolve() == src.resolve():
        raise SystemExit("refusing to overwrite the original results file")

    with src.open() as fh:
        rows = list(csv.DictReader(fh))
    print(f"{len(rows)} runs to replay")

    machines = sorted({r["machine_id"] for r in rows})
    manifests = {m: load_manifest(str(HERE / "machine_manifests.json"), m) for m in machines}
    urls = {m: (HERE / f"baseline_{m.lower()}.html").as_uri() for m in machines}

    out_fh = dst.open("w", newline="")
    w = csv.DictWriter(out_fh, fieldnames=FIELDS)
    w.writeheader()

    missing = flipped = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        # one probe of each untouched baseline, used to detect relabelling and clipping
        refs = {m: baseline_reference(page, urls[m], manifests[m]) for m in machines}
        print(f"captured baseline reference for {', '.join(machines)}")

        for i, r in enumerate(rows, 1):
            stem = stem_of(r)
            meta_path = art / f"{stem}.json"
            if not meta_path.exists():
                missing += 1
                continue
            cfg = json.loads(meta_path.read_text()).get("config") or {}
            manifest, url, ref = manifests[r["machine_id"]], urls[r["machine_id"]], refs[r["machine_id"]]

            strict = enforce(page, url, cfg, manifest, mode="mandate", reference=ref)
            loose = enforce(page, url, cfg, manifest, mode="outcome", reference=ref)

            codes = set(strict["safety_violations"])
            row = {k: r[k] for k in CARRY}
            row.update({
                "committed_mandate": int(strict["committed"]),
                "safety_mandate": "|".join(strict["safety_violations"]),
                "committed_outcome": int(loose["committed"]),
                "safety_outcome": "|".join(loose["safety_violations"]),
                "integrity": "|".join(strict["integrity_violations"]),
                "authority_codes": "|".join(sorted(codes & AUTHORITY)),
                "outcome_codes": "|".join(sorted(codes - AUTHORITY - UNUSABLE)),
            })
            w.writerow(row)
            out_fh.flush()
            flipped += int(loose["committed"]) - int(strict["committed"])
            if i % 200 == 0:
                print(f"  {i}/{len(rows)}")
        browser.close()
    out_fh.close()

    print(f"\nwritten to {dst}")
    if missing:
        print(f"{missing} runs skipped: no artifact on disk")
    print(f"{flipped} runs commit under the outcome criterion but not under mandate")
    print(f"original {src} left untouched")


if __name__ == "__main__":
    main()
