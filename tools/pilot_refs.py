"""Pilot: compare current tags with two-step reference tagging on photos whose post names the answer.

    python tools/pilot_refs.py --n 150

Leaves data/images.json alone; writes data/raw/pilot_refs.json (not published).
"""
import argparse
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from common import DATA, ROOT, load_env, load_images, load_options, read_posts
from tag import build_questions
from tag_refs import Usage, load_refs, tag_two_step

KEYWORDS = {
    "paint": {"esker-silver": [r"esker"], "glacier-white": [r"glacier white"], "half-moon-grey": [r"half ?moon"],
              "forest-green": [r"forest green"], "midnight": [r"\bmidnight\b"], "catalina-cove": [r"catalina"],
              "launch-green": [r"launch green"], "borealis": [r"borealis"]},
    "interior": {"black-crater": [r"black crater"], "coastal-cloud": [r"coastal cloud"]},
    "wheels": {"21-liquid-tungsten": [r"\b21 ?(\"|”|''|s\b|in\b|inch)", r"tungsten"],
               "20-black-sand-at": [r"\b20 ?(\"|”|''|s\b|in\b|inch)", r"all[- ]terrain", r"\bA/?Ts?\b", r"black sand"]},
}
VIEWS = {"paint": ("exterior",), "interior": ("interior",), "wheels": ("exterior", "detail")}
QUOTA = {"paint": 13, "interior": 12, "wheels": 22}  # per option


def answer_key(post):
    text = f"{post['title']} {post.get('text', '')}"
    key = {}
    for g, opts in KEYWORDS.items():
        hit = [v for v, pats in opts.items() if any(re.search(p, text, re.I) for p in pats)]
        if len(hit) == 1:
            key[g] = hit[0]
    return key


def shown(r):
    pii = r.get("pii") or {}
    return (r.get("thumb") and r["view"] != "other" and (r.get("render") or 0) < 0.8
            and pii.get("face", 0) < 0.5 and pii.get("info", 0) < 0.5)


def score(rows, field):
    """Per group/option: correct, wrong, abstained (unknown or under 60%)."""
    out = defaultdict(Counter)
    for row in rows:
        for g, truth in row["key"].items():
            t = row[field]["tags"][g] if field != "before" else row["before"][g]
            ans = t["value"] if t["value"] != "unknown" and t["confidence"] >= 0.6 else None
            res = "abstain" if ans is None else ("correct" if ans == truth else "wrong")
            out[(g, truth)][res] += 1
            out[(g, "ALL")][res] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Add OPENAI_API_KEY to .env.")

    posts, doc, options = read_posts(), load_images(), load_options()
    ref_posts = {m["post_id"] for m in json.loads((ROOT / "references" / "references.json").read_text(encoding="utf-8"))}
    pool = []
    for r in doc["images"]:
        p = posts.get(r["post_id"])
        if not p or r["post_id"] in ref_posts or not shown(r):
            continue
        k = {g: v for g, v in answer_key(p).items() if r["view"] in VIEWS[g]}
        if k:
            pool.append((r, p, k))
    random.seed(42)
    random.shuffle(pool)
    taken, per_post, need = [], Counter(), Counter()
    for r, p, k in pool:  # fill quotas per keyed option, at most 2 photos per post
        if len(taken) >= args.n or per_post[r["post_id"]] >= 2:
            continue
        if any(need[(g, v)] < QUOTA[g] for g, v in k.items()):
            taken.append((r, p, k))
            per_post[r["post_id"]] += 1
            for g, v in k.items():
                need[(g, v)] += 1
    print(f"{len(pool)} keyed photos available; piloting {len(taken)}")

    view_q = build_questions(options)[1]
    refs, usage, rows, failed = load_refs(), Usage(), [], 0

    def run(r, p, k):
        img = Image.open(ROOT / r["thumb"]).convert("RGB")
        return {"id": r["id"], "thumb": r["thumb"], "key": k,
                "before": {g: r["tags"][g] for g in k}, "after": tag_two_step(p, img, options, view_q, refs, key, usage)}

    with ThreadPoolExecutor(args.workers) as pool_x:
        futs = [pool_x.submit(run, *t) for t in taken]
        for n, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception as e:
                failed += 1
                print(f"  FAILED: {e}")
            if n % 25 == 0:
                print(f"  {n}/{len(taken)}  {usage}")
    for row in rows:  # step-1-only result, for comparison
        row["step1"] = {"tags": row["after"]["step1"]}

    (DATA / "raw").mkdir(exist_ok=True)
    (DATA / "raw" / "pilot_refs.json").write_text(json.dumps({"usage": vars(usage), "rows": rows}, indent=1), encoding="utf-8")
    sb, s1, sa = score(rows, "before"), score(rows, "step1"), score(rows, "after")
    print(f"\nPiloted {len(rows)}, failed {failed}. Usage: {usage}")
    print(f"{'group/option':34} {'before':>18} {'step 1 only':>18} {'with refs':>18}   (correct/wrong/abstain)")
    for k in sorted(sb, key=lambda k: (k[0], k[1] != "ALL", k[1])):
        f = lambda c: f"{c['correct']}/{c['wrong']}/{c['abstain']}"
        print(f"{k[0] + ' ' + k[1]:34} {f(sb[k]):>18} {f(s1[k]):>18} {f(sa[k]):>18}")
    lights = Counter(row["after"].get("light") for row in rows)
    print("light:", dict(lights))


if __name__ == "__main__":
    main()
