"""
Export run artifacts as a self-contained static site.

Produces, under --out (default: docs/, which GitHub Pages serves directly):
  runs/<stem>.html   baseline with the generated stylesheet inlined, panels applied
  compare.html       free-form vs skill-based side by side for one profile+machine
  index.html         browsable table of every run with its verdict

Each page is standalone: no build step, no external assets.

Usage:
    python export_gallery.py
    python export_gallery.py --out docs --results results.csv
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import pathlib
import re

HERE = pathlib.Path(__file__).parent

PAGE_CSS = """
:root{color-scheme:light}
body{margin:0;font:15px/1.6 system-ui,sans-serif;color:#16181d;background:#f7f8fa}
header{padding:20px 28px;background:#fff;border-bottom:1px solid #d8dbe1}
h1{margin:0 0 4px;font-size:20px;font-weight:600}
.sub{color:#5a606b;font-size:14px}
main{padding:24px 28px;max-width:1400px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #e4e7ec;vertical-align:top}
th{background:#eef0f4;font-weight:600;position:sticky;top:0}
tr:hover td{background:#f4f6f9}
code{font:13px ui-monospace,monospace;background:#eef0f4;padding:1px 5px}
.tag{display:inline-block;padding:1px 8px;font-size:12px;font-weight:600;white-space:nowrap}
.pass{background:#dff0d8;color:#2b5320}
.fail{background:#f7d9d9;color:#6b1c1c}
.warnv{background:#fbeccd;color:#6b4708}
a{color:#1b4f8f}
.controls{margin-bottom:14px;display:flex;gap:10px;flex-wrap:wrap}
select,input{font:14px system-ui;padding:5px 8px;border:1px solid #c8ccd4;background:#fff}
.frame{border:1px solid #d8dbe1;background:#fff;width:100%;height:660px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.panelhead{display:flex;justify-content:space-between;align-items:baseline;margin:0 0 6px}
.panelhead h2{font-size:15px;font-weight:600;margin:0}
details{background:#fff;border:1px solid #d8dbe1;padding:10px 12px;margin-top:12px}
pre{margin:8px 0 0;font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap;
    background:#f4f6f9;padding:10px;max-height:320px;overflow:auto}
"""


def verdict_tag(safety: str, integrity: str) -> str:
    if safety:
        return f'<span class="tag fail">{html.escape(safety.replace("|", ", "))}</span>'
    if integrity:
        return f'<span class="tag warnv">{html.escape(integrity.replace("|", ", "))}</span>'
    return '<span class="tag pass">committed</span>'


def render_run(baseline_html: str, css: str, panels_hidden: list[str]) -> str:
    """Inline the generated stylesheet into the baseline, exactly as the harness applied it."""
    hide = "".join(f'[data-panel="{p}"]{{display:none}}' for p in panels_hidden)
    block = f'<style id="adaptation">\n{css}\n{hide}\n</style>'
    return re.sub(r'<style id="adaptation">\s*</style>', lambda _: block,
                  baseline_html, count=1)


def load_rows(results: pathlib.Path) -> list[dict]:
    with results.open() as fh:
        return list(csv.DictReader(fh))


def stem_of(r: dict) -> str:
    return (f"{r['model'].replace(':', '-')}_{r['machine_id']}_"
            f"{r['profile_id']}_{r['arm']}_r{r['rep']}")


def build_index(rows: list[dict], pairs: dict) -> str:
    body = []
    for r in rows:
        stem = stem_of(r)
        cmp_key = (r["model"], r["machine_id"], r["profile_id"], r["rep"])
        cmp_link = (f' &middot; <a href="compare.html?k={html.escape("|".join(cmp_key))}">compare</a>'
                    if len(pairs.get(cmp_key, ())) == 2 else "")
        body.append(
            f'<tr data-arm="{r["arm"]}" data-model="{html.escape(r["model"])}" '
            f'data-machine="{r["machine_id"]}" data-k="{r["k"]}">'
            f'<td><code>{r["profile_id"]}</code></td><td>{r["k"]}</td>'
            f'<td>{r["machine_id"]}</td><td>{r["arm"].replace("_", "-")}</td>'
            f'<td>{html.escape(r["model"])}</td><td>{r["rep"]}</td>'
            f'<td>{verdict_tag(r["safety_violations"], r["integrity_violations"])}</td>'
            f'<td>{r["gen_seconds"]}s</td>'
            f'<td><a href="runs/{stem}.html">view</a>{cmp_link}</td></tr>')

    opts = lambda vals: "".join(f'<option>{html.escape(v)}</option>' for v in sorted(vals))
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Generated HMI adaptations</title><style>{PAGE_CSS}</style></head><body>
<header><h1>Generated HMI adaptations</h1>
<div class="sub">{len(rows)} runs &middot; each page is the baseline interface with the
model-generated stylesheet applied, exactly as the enforcement layer rendered it</div></header>
<main>
<div class="controls">
  <select id="f-arm"><option value="">all arms</option>{opts({r['arm'] for r in rows})}</select>
  <select id="f-model"><option value="">all models</option>{opts({r['model'] for r in rows})}</select>
  <select id="f-machine"><option value="">all machines</option>{opts({r['machine_id'] for r in rows})}</select>
  <select id="f-k"><option value="">all k</option>{opts({r['k'] for r in rows})}</select>
</div>
<table><thead><tr><th>profile</th><th>k</th><th>machine</th><th>arm</th><th>model</th>
<th>rep</th><th>verdict</th><th>gen</th><th></th></tr></thead>
<tbody id="rows">{''.join(body)}</tbody></table>
</main>
<script>
const sels = {{arm:'f-arm', model:'f-model', machine:'f-machine', k:'f-k'}};
function apply() {{
  const want = Object.fromEntries(Object.entries(sels)
    .map(([k,id]) => [k, document.getElementById(id).value]));
  for (const tr of document.querySelectorAll('#rows tr')) {{
    tr.style.display = Object.entries(want)
      .every(([k,v]) => !v || tr.dataset[k] === v) ? '' : 'none';
  }}
}}
Object.values(sels).forEach(id => document.getElementById(id).addEventListener('change', apply));
</script>
</body></html>"""


def build_compare(pairs: dict) -> str:
    index = {"|".join(k): {arm: stem for arm, stem in v.items()} for k, v in pairs.items()
             if len(v) == 2}
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Arm comparison</title><style>{PAGE_CSS}</style></head><body>
<header><h1>Free-form vs skill-based</h1>
<div class="sub" id="what">same profile, same machine, same model &mdash; the only
difference is whether the skill library was supplied</div></header>
<main>
<div class="controls"><select id="pick"></select>
<a href="index.html">back to index</a></div>
<div class="grid">
  <div><div class="panelhead"><h2>free-form</h2><span id="v-a"></span></div>
    <iframe class="frame" id="a"></iframe></div>
  <div><div class="panelhead"><h2>skill-based</h2><span id="v-b"></span></div>
    <iframe class="frame" id="b"></iframe></div>
</div>
</main>
<script>
const IDX = {json.dumps(index)};
const pick = document.getElementById('pick');
for (const k of Object.keys(IDX)) {{
  const o = document.createElement('option'); o.value = k;
  o.textContent = k.split('|').join(' \\u00b7 '); pick.appendChild(o);
}}
function show(k) {{
  const e = IDX[k]; if (!e) return;
  document.getElementById('a').src = 'runs/' + e.free_form + '.html';
  document.getElementById('b').src = 'runs/' + e.skill_based + '.html';
  document.getElementById('what').textContent = k.split('|').join(' \\u00b7 ');
}}
const q = new URLSearchParams(location.search).get('k');
pick.value = (q && IDX[q]) ? q : pick.options[0]?.value || '';
pick.addEventListener('change', () => show(pick.value));
if (pick.value) show(pick.value);
</script>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results.csv")
    ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--out", default="docs")
    args = ap.parse_args()

    results = HERE / args.results
    if not results.exists():
        raise SystemExit(f"{results} not found — run the campaign first")
    art = HERE / args.artifacts
    out = HERE / args.out
    (out / "runs").mkdir(parents=True, exist_ok=True)

    baselines = {m: (HERE / f"baseline_{m.lower()}.html").read_text()
                 for m in ["M1", "M2", "M3", "M4"]
                 if (HERE / f"baseline_{m.lower()}.html").exists()}

    rows = load_rows(results)
    pairs: dict[tuple, dict[str, str]] = {}
    written = 0
    for r in rows:
        stem = stem_of(r)
        meta_path = art / f"{stem}.json"
        css = ""
        panels: list[str] = []
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            cfg = meta.get("config") or {}
            css = cfg.get("css", "") or ""
            panels = cfg.get("panels_hidden", []) or []
        elif (art / f"{stem}.css").exists():
            css = (art / f"{stem}.css").read_text()
            panels = [p for p in (r.get("panels_hidden") or "").split("|") if p]
        else:
            continue

        base = baselines.get(r["machine_id"])
        if base is None:
            continue
        (out / "runs" / f"{stem}.html").write_text(render_run(base, css, panels))
        written += 1
        pairs.setdefault((r["model"], r["machine_id"], r["profile_id"], r["rep"]),
                         {})[r["arm"]] = stem

    (out / "index.html").write_text(build_index(rows, pairs))
    (out / "compare.html").write_text(build_compare(pairs))
    n_pairs = sum(1 for v in pairs.values() if len(v) == 2)
    print(f"{written} run pages, {n_pairs} arm comparisons -> {out}/index.html")
    if written < len(rows):
        print(f"note: {len(rows) - written} rows had no artifact "
              f"(runs made before artifact saving was added)")


if __name__ == "__main__":
    main()
