"""
Preflight checks. Run this before any campaign.

Verifies, in order: python packages, browser launch, fixtures present, checker
correctness, sampler coverage, Ollama reachability, required models pulled,
context window sufficiency, and one real end-to-end generation per model.

Exit code 0 means the campaign can start.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).parent
MACHINES = ["M1", "M2", "M3", "M4"]

ok_count, fail_count, warn_count = 0, 0, 0


def ok(msg: str) -> None:
    global ok_count
    ok_count += 1
    print(f"  \033[32mok\033[0m    {msg}")


def warn(msg: str) -> None:
    global warn_count
    warn_count += 1
    print(f"  \033[33mwarn\033[0m  {msg}")


def fail(msg: str) -> None:
    global fail_count
    fail_count += 1
    print(f"  \033[31mFAIL\033[0m  {msg}")


def section(title: str) -> None:
    print(f"\n{title}")


def check_packages() -> bool:
    section("packages")
    good = True
    for mod in ("playwright", "tinycss2"):
        try:
            m = __import__(mod)
            ok(f"{mod} {getattr(m, '__version__', getattr(m, 'VERSION', ''))}")
        except ImportError:
            fail(f"{mod} not installed — run setup.sh")
            good = False
    return good


def check_fixtures() -> bool:
    section("fixtures")
    good = True
    for name in ["machine_manifests.json", "skills.json", "checker.py",
                 "profiles.py", "runner.py"]:
        if (HERE / name).exists():
            ok(name)
        else:
            fail(f"{name} missing")
            good = False
    for m in MACHINES:
        p = HERE / f"baseline_{m.lower()}.html"
        if p.exists():
            ok(p.name)
        else:
            fail(f"{p.name} missing")
            good = False
    return good


def check_browser_and_checker() -> bool:
    section("browser and enforcement layer")
    try:
        from playwright.sync_api import sync_playwright
        from checker import enforce, load_manifest
    except Exception as e:
        fail(f"import failed: {e}")
        return False

    mf = str(HERE / "machine_manifests.json")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            ok("chromium launched")
            good = True
            for mid in MACHINES:
                m = load_manifest(mf, mid)
                url = (HERE / f"baseline_{mid.lower()}.html").as_uri()
                r = enforce(page, url, {"css": ""}, m)
                if r["committed"] and not r["safety_violations"]:
                    ok(f"{mid} baseline commits clean")
                else:
                    fail(f"{mid} baseline reports {r['safety_violations']} — false positive")
                    good = False
            # negative control: a known violation must be caught
            m = load_manifest(mf, "M1")
            url = (HERE / "baseline_m1.html").as_uri()
            r = enforce(page, url, {"css": "#emergency-stop{width:20px;height:20px}"}, m)
            if not r["committed"]:
                ok("known violation rejected")
            else:
                fail("known violation NOT caught — checker is not discriminating")
                good = False
            browser.close()
        return good
    except Exception as e:
        fail(f"browser check failed: {e}")
        print("        on a headless server try:  playwright install-deps chromium")
        return False


def check_sampler() -> bool:
    section("profile sampler")
    try:
        from profiles import build_profiles, coverage_report
        a = build_profiles()
        b = build_profiles()
        if [p["profile_id"] for p in a] != [p["profile_id"] for p in b]:
            fail("sampler is not deterministic")
            return False
        rep = coverage_report(a)
        ok(f"{rep['profiles']} profiles, per_k={rep['per_k']}")
        lo = min(rep["per_requirement"].values())
        hi = max(rep["per_requirement"].values())
        (ok if hi - lo <= 4 else warn)(f"requirement coverage {lo}-{hi} per requirement")
        return True
    except Exception as e:
        fail(f"sampler failed: {e}")
        return False


def ollama_models() -> list[str] | None:
    from runner import api
    try:
        with urllib.request.urlopen(api("/api/tags"), timeout=10) as r:
            return [m["name"] for m in json.loads(r.read())["models"]]
    except (urllib.error.URLError, TimeoutError, KeyError):
        return None


def check_models(models: list[str], num_ctx: int) -> bool:
    section("ollama")
    available = ollama_models()
    if available is None:
        from runner import OLLAMA_HOST
        fail(f"cannot reach ollama at {OLLAMA_HOST}")
        print("        check: the host is up, port 11434 is reachable, and the")
        print("        server was started with OLLAMA_HOST=0.0.0.0 so it accepts")
        print("        remote connections rather than binding to loopback only")
        return False
    ok(f"reachable, {len(available)} models present")
    good = True
    for m in models:
        if m in available:
            ok(f"{m} pulled")
        else:
            fail(f"{m} not pulled — run: ollama pull {m}")
            good = False
    return good


def check_prompt_fit(num_ctx: int) -> bool:
    section("prompt size")
    try:
        from checker import load_manifest
        from profiles import build_profiles
        from runner import build_prompt, extract_sources
    except Exception as e:
        fail(f"import failed: {e}")
        return False

    skills = json.loads((HERE / "skills.json").read_text())
    worst = 0
    for mid in MACHINES:
        css, html = extract_sources(HERE / f"baseline_{mid.lower()}.html")
        m = load_manifest(str(HERE / "machine_manifests.json"), mid)
        for prof in build_profiles():
            if prof["k"] != 4:
                continue
            n = len(build_prompt(prof, m, css, html, "skill_based", skills))
            worst = max(worst, n)
    est = int(worst / 3.6)
    if est < num_ctx * 0.8:
        ok(f"largest prompt ~{est} tokens, fits num_ctx={num_ctx}")
        return True
    fail(f"largest prompt ~{est} tokens vs num_ctx={num_ctx} — raise --num-ctx")
    return False


def check_generation(models: list[str], num_ctx: int, timeout: int) -> bool:
    section("end-to-end generation")
    try:
        from checker import load_manifest
        from profiles import build_profiles
        from runner import build_prompt, extract_sources, generate, parse_config
    except Exception as e:
        fail(f"import failed: {e}")
        return False

    skills = json.loads((HERE / "skills.json").read_text())
    css, html = extract_sources(HERE / "baseline_m1.html")
    m = load_manifest(str(HERE / "machine_manifests.json"), "M1")
    prof = next(p for p in build_profiles() if p["k"] == 2)
    prompt = build_prompt(prof, m, css, html, "skill_based", skills)

    good = True
    for model in models:
        try:
            raw, secs = generate(model, prompt, 0.3, timeout, num_ctx)
        except Exception as e:
            fail(f"{model}: generation failed ({e})")
            good = False
            continue
        cfg = parse_config(raw)
        if cfg is None or "css" not in cfg:
            fail(f"{model}: output not parseable as a config ({secs:.0f}s, {len(raw)} chars)")
            print(f"        first 200 chars: {raw[:200]!r}")
            good = False
            continue
        thinking = "<think>" in raw.lower()
        ok(f"{model}: parsed in {secs:.0f}s, {len(raw)} chars")
        if thinking:
            warn(f"{model}: emitted a thinking block — disable it to cut generation time")
        est_hours = secs * 45 * 4 * 2 * 3 / 3600
        print(f"        projected full campaign for this model: ~{est_hours:.1f} h")
    return good


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["gemma4:e4b", "gemma4:31b"])
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--host", default=None,
                    help="ollama endpoint, e.g. 192.168.1.50 or http://gpu-01:11434")
    ap.add_argument("--skip-generation", action="store_true")
    args = ap.parse_args()
    from runner import set_host, OLLAMA_HOST as _h
    host = set_host(args.host) if args.host else _h
    print(f"ollama endpoint: {host}")

    print(f"preflight — python {sys.version.split()[0]} — {HERE}")
    results = [
        check_packages(),
        check_fixtures(),
        check_browser_and_checker(),
        check_sampler(),
        check_prompt_fit(args.num_ctx),
    ]
    if check_models(args.models, args.num_ctx) and not args.skip_generation:
        results.append(check_generation(args.models, args.num_ctx, args.timeout))
    else:
        results.append(False)

    print(f"\n{ok_count} ok, {warn_count} warnings, {fail_count} failures")
    if all(results):
        print("\nready. pilot:")
        print(f"  python runner.py --models {args.models[0]} "
              f"--num-ctx {args.num_ctx} --limit 20")
        return 0
    print("\nnot ready — resolve the failures above before running the campaign.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
