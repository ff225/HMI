"""Every unmodified baseline must commit clean, and benign adaptations must pass."""
import pathlib
from playwright.sync_api import sync_playwright
from checker import enforce, load_manifest

HERE = pathlib.Path(__file__).parent
MF = str(HERE / "machine_manifests.json")
CASES = [
    ("unmodified", {"css": ""}),
    ("magnification 1.4", {"css": ":root{--font-scale:1.4;--line-height:1.6}"}),
    ("coarse pointing", {"css": ":root{--touch-target-min:72px;--gap:20px}"}),
]
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()
    for mid in ["M1", "M2", "M3", "M4"]:
        m = load_manifest(MF, mid)
        url = (HERE / f"baseline_{mid.lower()}.html").as_uri()
        for label, cfg in CASES:
            r = enforce(page, url, cfg, m)
            s, i = r["safety_violations"], r["integrity_violations"]
            tag = "OK " if not s else "BAD"
            print(f"{tag} {mid} {label:<18} committed={str(r['committed']):<5} safety={s or '-'} integrity={i or '-'}")
    b.close()
