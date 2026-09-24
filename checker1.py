"""
Deterministic enforcement layer.

Two stages:
  pre-render  -- syntactic filtering of the candidate stylesheet against the
                 machine's declared adaptable surface
  post-render -- measurement of the rendered result: safety element geometry,
                 occlusion via hit-testing, resolved contrast, layout integrity

A configuration is accepted only if no SAFETY violation is found. Integrity
violations are reported separately and drive one regeneration attempt.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, asdict

import tinycss2

SAFETY_CODES = {
    "parse_error", "schema_error", "forbidden_property", "protected_selector",
    "range_violation", "safety_size", "safety_position", "safety_occlusion",
    "safety_contrast", "panel_lock",
}
INTEGRITY_CODES = {"integrity_overflow", "integrity_overlap", "integrity_truncation"}


@dataclass
class Violation:
    code: str
    stage: str
    detail: str

    @property
    def is_safety(self) -> bool:
        return self.code in SAFETY_CODES


# --------------------------------------------------------------------------
# pre-render
# --------------------------------------------------------------------------

def _selector_text(prelude) -> str:
    return tinycss2.serialize(prelude).strip()


def check_pre_render(css: str, config: dict, manifest: dict) -> list[Violation]:
    v: list[Violation] = []
    surface = manifest["adaptable_surface"]
    allowed = set(surface["properties"])
    forbidden = set(surface["forbidden_properties"])
    protected = surface["protected_selectors"]

    if not isinstance(config, dict) or "css" not in config:
        return [Violation("schema_error", "pre-render", "missing 'css' key")]
    if not isinstance(config.get("panels_hidden", []), list):
        v.append(Violation("schema_error", "pre-render", "'panels_hidden' is not a list"))

    rules, err = tinycss2.parse_stylesheet(css, skip_whitespace=True), None
    for rule in rules:
        if rule.type == "error":
            v.append(Violation("parse_error", "pre-render", rule.message))
            continue
        if rule.type != "qualified-rule":
            continue

        sel = _selector_text(rule.prelude)
        for p in protected:
            if p in sel:
                v.append(Violation("protected_selector", "pre-render",
                                   f"rule targets protected selector: {sel}"))
                break

        for decl in tinycss2.parse_blocks_contents(rule.content):
            if decl.type == "error":
                v.append(Violation("parse_error", "pre-render", decl.message))
                continue
            if decl.type != "declaration":
                continue
            name = decl.lower_name
            if name.startswith("--"):
                continue
            if name in forbidden:
                v.append(Violation("forbidden_property", "pre-render",
                                   f"{name} in rule '{sel}'"))
            elif name not in allowed:
                v.append(Violation("forbidden_property", "pre-render",
                                   f"{name} is outside the adaptable surface ('{sel}')"))

    v.extend(_check_ranges(css, manifest))

    hideable = set(manifest["adaptable_surface"]["panel_visibility"]["hideable"])
    locked = set(manifest["adaptable_surface"]["panel_visibility"]["locked"])
    for panel in config.get("panels_hidden", []):
        if panel in locked:
            v.append(Violation("panel_lock", "pre-render", f"locked panel hidden: {panel}"))
        elif panel not in hideable:
            v.append(Violation("schema_error", "pre-render", f"unknown panel: {panel}"))
    return v


_TOKEN_RANGE = {
    "--font-scale": "font-scale",
    "--touch-target-min": "touch-target-min-px",
    "--line-height": "line-height",
}


def _check_ranges(css: str, manifest: dict) -> list[Violation]:
    v: list[Violation] = []
    ranges = manifest["adaptable_surface"]["ranges"]
    for token, key in _TOKEN_RANGE.items():
        if key not in ranges:
            continue
        lo, hi = ranges[key]
        for raw in re.findall(rf"{token}\s*:\s*([^;}}]+)", css):
            m = re.search(r"-?\d+(?:\.\d+)?", raw)
            if not m:
                continue
            val = float(m.group())
            if val < lo or val > hi:
                v.append(Violation("range_violation", "pre-render",
                                   f"{token}={val} outside [{lo}, {hi}]"))
    return v


# --------------------------------------------------------------------------
# post-render
# --------------------------------------------------------------------------

_PROBE = r"""
(() => {
  const srgb = c => { c /= 255; return c <= 0.03928 ? c/12.92 : Math.pow((c+0.055)/1.055, 2.4); };
  const lum = ([r,g,b]) => 0.2126*srgb(r) + 0.7152*srgb(g) + 0.0722*srgb(b);
  const parse = s => { const m = s.match(/[\d.]+/g); return m ? m.slice(0,3).map(Number) : null; };
  const alpha = s => { const m = s.match(/[\d.]+/g); return m && m.length > 3 ? Number(m[3]) : 1; };

  const resolvedBg = el => {
    let n = el;
    while (n && n !== document.documentElement) {
      const cs = getComputedStyle(n);
      if (alpha(cs.backgroundColor) > 0) return parse(cs.backgroundColor);
      n = n.parentElement;
    }
    return [255,255,255];
  };
  const contrast = el => {
    const fg = parse(getComputedStyle(el).color);
    const bg = resolvedBg(el);
    const a = lum(fg), b = lum(bg);
    return (Math.max(a,b) + 0.05) / (Math.min(a,b) + 0.05);
  };

  const probe = sel => {
    const els = [...document.querySelectorAll(sel)];
    return els.map(el => {
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const cx = r.left + r.width/2, cy = r.top + r.height/2;
      const dx = r.width/4, dy = r.height/4;
      const pts = [
        [cx, cy],
        [cx - dx, cy - dy], [cx + dx, cy - dy],
        [cx - dx, cy + dy], [cx + dx, cy + dy],
      ];
      const occluded = pts.filter(([x,y]) => {
        const hit = document.elementFromPoint(x, y);
        return !(hit && (hit === el || el.contains(hit) || hit.contains(el)));
      }).length;
      return {
        w: r.width, h: r.height, x: r.left, y: r.top, right: r.right, bottom: r.bottom,
        display: cs.display, visibility: cs.visibility, opacity: Number(cs.opacity),
        occluded, contrast: contrast(el),
      };
    });
  };

  const clipped = [...document.querySelectorAll('.metric-value,.metric-label,.control')]
    .filter(el => el.scrollWidth > el.clientWidth + 1)
    .map(el => el.id || el.className);

  const boxes = [...document.querySelectorAll('.control')].map(el => {
    const r = el.getBoundingClientRect();
    return {id: el.id, l: r.left, t: r.top, r: r.right, b: r.bottom};
  });
  const overlaps = [];
  for (let i = 0; i < boxes.length; i++)
    for (let j = i+1; j < boxes.length; j++) {
      const a = boxes[i], c = boxes[j];
      if (a.l < c.r - 1 && c.l < a.r - 1 && a.t < c.b - 1 && c.t < a.b - 1)
        overlaps.push(`${a.id}~${c.id}`);
    }

  const de = document.documentElement;
  return {
    probe: Object.fromEntries(SELECTORS.map(s => [s, probe(s)])),
    panels: Object.fromEntries([...document.querySelectorAll('[data-panel]')]
      .map(el => [el.dataset.panel, el.getBoundingClientRect().height > 0])),
    overflowX: Math.max(de.scrollWidth - de.clientWidth, document.body.scrollWidth - document.body.clientWidth),
    overflowY: Math.max(de.scrollHeight - de.clientHeight, document.body.scrollHeight - document.body.clientHeight),
    viewport: {w: window.innerWidth, h: window.innerHeight},
    clipped, overlaps,
  };
})()
"""


def _anchor_ok(box: dict, region: dict, vp: dict) -> bool:
    margin, tol = region["margin_px"], region["tolerance_px"]
    a = region["anchor"]
    if a == "bottom-right":
        return (abs((vp["w"] - box["right"]) - margin) <= tol
                and abs((vp["h"] - box["bottom"]) - margin) <= tol)
    if a == "left-rail":
        return abs(box["x"] - margin) <= tol
    if a == "control-bar":
        return True  # positional containment checked by occlusion + viewport bounds
    return True


def check_post_render(page, manifest: dict) -> list[Violation]:
    selectors = [e["selector"] for e in manifest["safety_elements"]]
    data = page.evaluate(_PROBE.replace("SELECTORS", json.dumps(selectors)))
    v: list[Violation] = []
    vp = data["viewport"]

    for elem in manifest["safety_elements"]:
        sel, name = elem["selector"], elem["element_id"]
        boxes = data["probe"].get(sel, [])
        if not boxes:
            v.append(Violation("safety_position", "post-render", f"{name}: not present"))
            continue
        for b in boxes:
            if b["display"] == "none" or b["visibility"] == "hidden" or b["opacity"] < 0.9:
                v.append(Violation("safety_position", "post-render", f"{name}: not visible"))
                continue
            mw, mh = elem["min_size_px"]
            if b["w"] < mw - 0.5 or b["h"] < mh - 0.5:
                v.append(Violation("safety_size", "post-render",
                                   f"{name}: {b['w']:.0f}x{b['h']:.0f} < {mw}x{mh}"))
            if b["x"] < 0 or b["y"] < 0 or b["right"] > vp["w"] or b["bottom"] > vp["h"]:
                v.append(Violation("safety_position", "post-render",
                                   f"{name}: outside viewport"))
            elif "allowed_region" in elem and not _anchor_ok(b, elem["allowed_region"], vp):
                v.append(Violation("safety_position", "post-render",
                                   f"{name}: outside allowed region"))
            if b["occluded"]:
                v.append(Violation("safety_occlusion", "post-render",
                                   f"{name}: {b['occluded']}/5 probe points covered"))
            if b["contrast"] < elem["min_contrast_ratio"]:
                v.append(Violation("safety_contrast", "post-render",
                                   f"{name}: contrast {b['contrast']:.2f} < {elem['min_contrast_ratio']}"))

    for panel in manifest["adaptable_surface"]["panel_visibility"]["locked"]:
        if not data["panels"].get(panel, False):
            v.append(Violation("panel_lock", "post-render", f"locked panel not rendered: {panel}"))

    if data["overflowX"] > 1 or data["overflowY"] > 1:
        v.append(Violation("integrity_overflow", "post-render",
                           f"overflow x={data['overflowX']} y={data['overflowY']}"))
    if data["overlaps"]:
        v.append(Violation("integrity_overlap", "post-render", ", ".join(data["overlaps"])))
    if data["clipped"]:
        v.append(Violation("integrity_truncation", "post-render", ", ".join(data["clipped"][:4])))
    return v


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def enforce(page, baseline_url: str, config: dict, manifest: dict) -> dict:
    """Apply a candidate configuration on a staging render and verify it."""
    css = config.get("css", "") if isinstance(config, dict) else ""
    violations = check_pre_render(css, config, manifest)
    if any(x.code == "parse_error" for x in violations):
        return _verdict(violations, committed=False)

    vpt = manifest["viewport"]
    page.set_viewport_size({"width": vpt["width"], "height": vpt["height"]})
    page.goto(baseline_url)
    page.evaluate(
        "([css, hidden]) => {"
        "  document.getElementById('adaptation').textContent = css;"
        "  document.querySelectorAll('[data-panel]').forEach(el => el.dataset.hidden ="
        "    hidden.includes(el.dataset.panel) ? 'true' : 'false');"
        "}",
        [css, config.get("panels_hidden", [])],
    )
    violations += check_post_render(page, manifest)
    return _verdict(violations, committed=not any(x.is_safety for x in violations))


def _verdict(violations: list[Violation], committed: bool) -> dict:
    return {
        "committed": committed,
        "safety_violations": sorted({x.code for x in violations if x.is_safety}),
        "integrity_violations": sorted({x.code for x in violations if not x.is_safety}),
        "detail": [asdict(x) for x in violations],
    }


def load_manifest(path: str, machine_id: str) -> dict:
    data = json.loads(pathlib.Path(path).read_text())
    return next(m for m in data["machines"] if m["machine_id"] == machine_id)
