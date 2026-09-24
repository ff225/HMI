"""
Experiment runner.

Both arms receive the same input (interaction requirements + machine manifest +
baseline DOM/CSS). The only manipulated factor is whether the skill library is
supplied. The checker always runs, on every arm, for measurement; what differs
between arm B and arm C is only whether its verdict is acted upon.

The checker, its violation taxonomy and the validation cases are never shown to
the model.

Usage:
    python3 runner.py --models qwen2.5:3b qwen2.5:14b --reps 3
    python3 runner.py --models qwen2.5:3b --limit 20        # pilot
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from checker import enforce, load_manifest
from export_gallery import render_run
from profiles import build_profiles

HERE = pathlib.Path(__file__).parent
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://155.185.217.9:11434")
MACHINES = ["M1", "M2", "M3", "M4"]
ARMS = ["free_form", "skill_based"]


def set_host(url: str) -> str:
    """Normalise a user-supplied host: add scheme and default port if missing."""
    global OLLAMA_HOST
    u = url.strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    tail = u.split("://", 1)[1]
    if u.startswith("http://") and ":" not in tail.split("/")[0]:
        u += ":11434"
    OLLAMA_HOST = u
    return OLLAMA_HOST


def api(path: str) -> str:
    return f"{OLLAMA_HOST}{path}"

FIELDS = [
    "profile_id", "k", "machine_id", "arm", "model", "rep",
    "gen_seconds", "output_chars", "parsed",
    "committed", "safety_violations", "integrity_violations", "worst_code",
    "font_scale", "target_min", "panels_hidden",
]

SEVERITY = [
    "parse_error", "schema_error", "safety_occlusion", "safety_position",
    "safety_size", "safety_contrast", "panel_lock", "protected_selector",
    "forbidden_property", "range_violation",
    "integrity_overflow", "integrity_overlap", "integrity_truncation",
]

COMMON = """You adapt an industrial machine interface to an operator's needs.

OPERATOR INTERACTION REQUIREMENTS
{profile}

MACHINE MANIFEST
{manifest}

BASELINE STYLESHEET
{base_css}

BASELINE MARKUP
{base_html}

Produce a stylesheet that adapts the interface to the stated requirements.
Requirements marked "persistent" reflect stable operator traits; requirements
marked "transient" reflect a momentary condition.

Respond with a single JSON object and nothing else, in this exact form:
{{"css": "<stylesheet text>", "panels_hidden": ["<panel name>", ...]}}
Use an empty list when no panel should be hidden.
"""

SKILL_BLOCK = """
ADAPTATION SKILL LIBRARY
{skills}

Select the skills whose applicability matches the stated requirements, choose
parameter values within the directive ranges for the given level, and compose
them into one configuration. Where two selected skills touch the same property,
resolve the conflict using the declared precedence order. Apply a skill's
degradation rule when the machine does not expose what the skill needs.
"""


def build_prompt(profile: dict, machine: dict, base_css: str, base_html: str,
                 arm: str, skills: dict) -> str:
    p = COMMON.format(
        profile=json.dumps(profile, indent=2),
        manifest=json.dumps(machine, indent=2),
        base_css=base_css.strip(),
        base_html=base_html.strip(),
    )
    if arm == "skill_based":
        p += SKILL_BLOCK.format(skills=json.dumps(skills, indent=2))
    return p


def extract_sources(path: pathlib.Path) -> tuple[str, str]:
    text = path.read_text()
    css = re.search(r'<style id="base">(.*?)</style>', text, re.S)
    body = re.search(r"<body>(.*?)</body>", text, re.S)
    return (css.group(1) if css else ""), (body.group(1) if body else "")


def generate(model: str, prompt: str, temperature: float, timeout: int,
             num_ctx: int) -> tuple[str, float]:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(api("/api/generate"), data=payload,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())["response"]
    return out, time.time() - t0


def parse_config(text: str) -> dict | None:
    """Recover the JSON object from the model output, tolerating fences and prose.

    Reasoning models emit a thinking block that often contains draft JSON and
    stray braces; it is discarded before parsing so only the final answer counts.
    """
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    t = re.sub(r"^.*?</think>", "", t, flags=re.S | re.I)  # unterminated block
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    candidates = [i for i, ch in enumerate(t) if ch == "{"]
    fallback = None
    for start in reversed(candidates):
        obj = _scan(t, start)
        if obj is None:
            continue
        if "css" in obj:
            return obj
        fallback = fallback or obj
    return fallback


def _scan(t: str, start: int) -> dict | None:
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def worst(codes: list[str]) -> str:
    for c in SEVERITY:
        if c in codes:
            return c
    return "-"


def token_of(css: str, name: str) -> str:
    m = re.search(rf"{name}\s*:\s*([^;}}]+)", css or "")
    return m.group(1).strip() if m else ""


def done_keys(path: pathlib.Path) -> set[tuple]:
    if not path.exists():
        return set()
    with path.open() as fh:
        return {(r["profile_id"], r["machine_id"], r["arm"], r["model"], r["rep"])
                for r in csv.DictReader(fh)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--machines", nargs="+", default=MACHINES)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--num-ctx", type=int, default=16384,
                    help="context window; must exceed the prompt or Ollama truncates silently")
    ap.add_argument("--limit", type=int, default=0, help="stop after N generations (pilot)")
    ap.add_argument("--profiles", nargs="+", default=None,
                    help="restrict to given profile ids (P007) and/or k values (k=4)")
    ap.add_argument("--arms", nargs="+", default=ARMS, choices=ARMS,
                    help="restrict to one arm")
    ap.add_argument("--host", default=OLLAMA_HOST,
                    help="ollama endpoint, e.g. 192.168.1.50 or http://gpu-01:11434")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--artifacts", default="artifacts",
                    help="directory for raw output, parsed config and verdict")
    ap.add_argument("--pages", action="store_true",
                    help="also write a standalone HTML page per run, viewable directly")
    ap.add_argument("--screenshots", action="store_true",
                    help="also save a PNG per run (large; only for paper figures)")
    args = ap.parse_args()
    set_host(args.host)
    print(f"ollama endpoint: {OLLAMA_HOST}")

    skills = json.loads((HERE / "skills.json").read_text())
    profiles = build_profiles()
    if args.profiles:
        ids = {s.upper() for s in args.profiles if not s.lower().startswith("k=")}
        ks = {int(s.split("=")[1]) for s in args.profiles if s.lower().startswith("k=")}
        profiles = [p for p in profiles
                    if p["profile_id"] in ids or p["k"] in ks]
        if not profiles:
            raise SystemExit(f"no profile matches {args.profiles}")
        print(f"selected {len(profiles)} profile(s): "
              f"{', '.join(p['profile_id'] for p in profiles)}")
    manifests = {m: load_manifest(str(HERE / "machine_manifests.json"), m) for m in args.machines}
    sources = {m: extract_sources(HERE / f"baseline_{m.lower()}.html") for m in args.machines}
    baseline_text = {m: (HERE / f"baseline_{m.lower()}.html").read_text() for m in args.machines}
    urls = {m: (HERE / f"baseline_{m.lower()}.html").as_uri() for m in args.machines}

    art = HERE / args.artifacts
    art.mkdir(exist_ok=True)
    if args.pages:
        (art / "pages").mkdir(exist_ok=True)

    out = HERE / args.out
    seen = done_keys(out)
    new = not out.exists()
    fh = out.open("a", newline="")
    w = csv.DictWriter(fh, fieldnames=FIELDS)
    if new:
        w.writeheader()

    n = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        for model in args.models:
            for mid in args.machines:
                for profile in profiles:
                    for arm in args.arms:
                        for rep in range(args.reps):
                            key = (profile["profile_id"], mid, arm, model, str(rep))
                            if key in seen:
                                continue
                            if args.limit and n >= args.limit:
                                fh.close(); browser.close()
                                print(f"\npilot limit reached: {n} generations")
                                return
                            css_src, html_src = sources[mid]
                            prompt = build_prompt(profile, manifests[mid],
                                                  css_src, html_src, arm, skills)
                            if len(prompt) / 3.6 > args.num_ctx * 0.8:
                                print(f"  prompt too long for num_ctx={args.num_ctx}; aborting")
                                fh.close(); browser.close(); return
                            try:
                                raw, secs = generate(model, prompt, args.temperature,
                                                     args.timeout, args.num_ctx)
                            except (urllib.error.URLError, TimeoutError) as e:
                                print(f"  generation failed ({e}); skipping")
                                continue
                            stem = (f"{model.replace(':', '-')}_{mid}_"
                                    f"{profile['profile_id']}_{arm}_r{rep}")
                            cfg = parse_config(raw)
                            if cfg is None:
                                verdict = {"committed": False,
                                           "safety_violations": ["parse_error"],
                                           "integrity_violations": []}
                                cfg = {}
                            else:
                                shot = str(art / f"{stem}.png") if args.screenshots else None
                                verdict = enforce(page, urls[mid], cfg, manifests[mid],
                                                  screenshot=shot)
                            codes = verdict["safety_violations"] + verdict["integrity_violations"]
                            (art / f"{stem}.json").write_text(json.dumps({
                                "profile": profile, "machine_id": mid, "arm": arm,
                                "model": model, "rep": rep,
                                "gen_seconds": round(secs, 2),
                                "raw_output": raw,
                                "config": cfg,
                                "verdict": verdict,
                            }, indent=2))
                            (art / f"{stem}.css").write_text(cfg.get("css", "") or "")
                            if args.pages and cfg:
                                (art / "pages" / f"{stem}.html").write_text(
                                    render_run(baseline_text[mid],
                                               cfg.get("css", "") or "",
                                               cfg.get("panels_hidden", []) or []))
                            w.writerow({
                                "profile_id": profile["profile_id"], "k": profile["k"],
                                "machine_id": mid, "arm": arm, "model": model, "rep": rep,
                                "gen_seconds": round(secs, 2), "output_chars": len(raw),
                                "parsed": int(bool(cfg)),
                                "committed": int(verdict["committed"]),
                                "safety_violations": "|".join(verdict["safety_violations"]),
                                "integrity_violations": "|".join(verdict["integrity_violations"]),
                                "worst_code": worst(codes),
                                "font_scale": token_of(cfg.get("css", ""), "--font-scale"),
                                "target_min": token_of(cfg.get("css", ""), "--touch-target-min"),
                                "panels_hidden": "|".join(cfg.get("panels_hidden", []) or []),
                            })
                            fh.flush()
                            n += 1
                            print(f"{n:5d} {model:<14} {mid} {profile['profile_id']} "
                                  f"k={profile['k']} {arm:<11} {secs:6.1f}s "
                                  f"{'commit' if verdict['committed'] else worst(codes)}")
        browser.close()
    fh.close()
    print(f"\n{n} generations written to {out}")


if __name__ == "__main__":
    main()
