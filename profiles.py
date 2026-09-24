"""
Systematic sampling of interaction-requirement profiles.

Coverage rule: for each k in 1..4 the sampler emits a fixed number of profiles,
balancing how often each requirement appears at each k. Sampling is seeded, so
the profile set is identical across arms, models and repetitions.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from itertools import combinations

VOCABULARY = [
    "magnification",
    "contrast-enhancement",
    "color-differentiation",
    "coarse-pointing",
    "reach-mirroring",
    "reading-support",
    "cognitive-load-reduction",
    "feedback-amplification",
    "alternative-input",
    "motion-reduction",
]

LEVELS = ["low", "moderate", "high"]

# Requirements that describe a momentary condition rather than a stable trait.
# Any of them may still be declared persistent; this only sets the sampling prior.
TRANSIENT_PRONE = {
    "cognitive-load-reduction",
    "feedback-amplification",
    "motion-reduction",
}

# Requirement pairs that cannot sensibly co-occur.
INCOMPATIBLE = {
    frozenset({"magnification", "cognitive-load-reduction"}): False,  # allowed, but tense
}

PER_K = {1: 10, 2: 12, 3: 12, 4: 11}  # 45 profiles


def _balanced_subsets(k: int, n: int, rng: random.Random) -> list[tuple[str, ...]]:
    """Pick n subsets of size k, keeping per-requirement frequency as flat as possible."""
    pool = list(combinations(VOCABULARY, k))
    rng.shuffle(pool)
    chosen: list[tuple[str, ...]] = []
    freq: Counter[str] = Counter()
    for _ in range(n):
        pool.sort(key=lambda c: (sum(freq[r] for r in c), rng.random()))
        pick = pool.pop(0)
        chosen.append(pick)
        freq.update(pick)
    return chosen


def build_profiles(seed: int = 20260101) -> list[dict]:
    rng = random.Random(seed)
    profiles: list[dict] = []
    idx = 0
    for k, n in PER_K.items():
        for subset in _balanced_subsets(k, n, rng):
            idx += 1
            reqs = []
            for name in subset:
                transient = rng.random() < (0.6 if name in TRANSIENT_PRONE else 0.15)
                reqs.append({
                    "need": name,
                    "level": rng.choice(LEVELS),
                    "validity": "transient" if transient else "persistent",
                })
            profiles.append({
                "profile_id": f"P{idx:03d}",
                "k": k,
                "requirements": sorted(reqs, key=lambda r: r["need"]),
                "locale": "en",
            })
    return profiles


def coverage_report(profiles: list[dict]) -> dict:
    per_req: Counter[str] = Counter()
    per_level: Counter[str] = Counter()
    per_k: Counter[int] = Counter()
    for p in profiles:
        per_k[p["k"]] += 1
        for r in p["requirements"]:
            per_req[r["need"]] += 1
            per_level[r["level"]] += 1
    return {
        "profiles": len(profiles),
        "per_k": dict(sorted(per_k.items())),
        "per_requirement": dict(sorted(per_req.items())),
        "per_level": dict(sorted(per_level.items())),
    }


if __name__ == "__main__":
    profiles = build_profiles()
    with open("profiles.json", "w") as fh:
        json.dump(profiles, fh, indent=2)
    print(json.dumps(coverage_report(profiles), indent=2))
    print("\nexample:\n" + json.dumps(profiles[20], indent=2))
