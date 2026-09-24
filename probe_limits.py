"""Do the fixtures actually break within their own declared ranges?"""
import pathlib
from playwright.sync_api import sync_playwright
from checker import enforce, load_manifest

HERE = pathlib.Path(__file__).parent
MF = str(HERE / "machine_manifests.json")
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()
    for mid in ["M1","M2","M3","M4"]:
        m = load_manifest(MF, mid)
        url = (HERE / f"baseline_{mid.lower()}.html").as_uri()
        fs_hi = m["adaptable_surface"]["ranges"]["font-scale"][1]
        tt_hi = m["adaptable_surface"]["ranges"]["touch-target-min-px"][1]
        cases = [
            (f"font-scale max {fs_hi}", {"css": f":root{{--font-scale:{fs_hi}}}"}),
            (f"target max {tt_hi}px",  {"css": f":root{{--touch-target-min:{tt_hi}px;--gap:28px;--pad:24px}}"}),
            ("combined max", {"css": f":root{{--font-scale:{fs_hi};--touch-target-min:{tt_hi}px;--line-height:2.0;--gap:28px;--pad:24px}}"}),
        ]
        for label, cfg in cases:
            r = enforce(page, url, cfg, m)
            print(f"{mid} {label:<20} committed={str(r['committed']):<5} safety={r['safety_violations'] or '-'} integrity={r['integrity_violations'] or '-'}")
    b.close()
