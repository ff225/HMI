"""Validate the enforcement layer against configurations with known violations."""

import pathlib
import sys

from playwright.sync_api import sync_playwright

from checker import enforce, load_manifest

HERE = pathlib.Path(__file__).parent
URL = (HERE / "baseline_m1.html").as_uri()
MANIFEST = load_manifest(str(HERE / "machine_manifests.json"), "M1")

CASES = [
    ("C00 unmodified baseline", {"css": ""}, set()),
    ("C01 benign magnification", {"css": ":root{--font-scale:1.4;--line-height:1.6}"}, set()),
    ("C02 benign coarse pointing",
     {"css": ":root{--touch-target-min:72px;--gap:20px}", "panels_hidden": ["maintenance"]}, set()),

    ("C03 forbidden property",
     {"css": ".panel{position:absolute;top:0}"}, {"forbidden_property"}),
    ("C04 protected selector",
     {"css": "#emergency-stop{min-width:20px}"}, {"protected_selector"}),
    ("C05 range violation",
     {"css": ":root{--font-scale:3.2}"}, {"range_violation"}),
    ("C06 locked panel hidden",
     {"css": "", "panels_hidden": ["warnings"]}, {"panel_lock"}),

    ("C07 e-stop shrunk",
     {"css": "#emergency-stop{width:40px;height:40px}"}, {"safety_size"}),
    ("C08 e-stop pushed off-screen",
     {"css": "#emergency-stop{right:-120px}"}, {"safety_position"}),
    ("C09 e-stop hidden",
     {"css": "#emergency-stop{display:none}"}, {"safety_position"}),
    ("C10 e-stop low contrast",
     {"css": "#emergency-stop{color:#c26a6a}"}, {"safety_contrast"}),
    ("C11 e-stop occluded by overlay",
     {"css": "#panels{position:fixed;inset:0;z-index:99;background:#fff}"}, {"safety_occlusion"}),
    ("C12 alarm banner shrunk",
     {"css": "#alarm-banner{min-width:100px;min-height:12px}"}, {"safety_size"}),

    ("C13 controls overflow",
     {"css": ".control{min-width:420px}"}, {"integrity_overflow"}),
    ("C14 metric text truncated",
     {"css": ".metric-value{font-size:4em}"}, {"integrity_truncation"}),
]


def main() -> int:
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for label, config, expected in CASES:
            r = enforce(page, URL, config, MANIFEST)
            found = set(r["safety_violations"]) | set(r["integrity_violations"])
            ok = expected.issubset(found) and (not expected or found) and \
                 (bool(expected) or not found)
            flag = "PASS" if ok else "FAIL"
            if not ok:
                failures += 1
            extra = found - expected
            print(f"{flag}  {label:<38} committed={str(r['committed']):<5} "
                  f"found={sorted(found) or '-'}"
                  + (f"  [extra: {sorted(extra)}]" if extra and ok else ""))
        browser.close()
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
