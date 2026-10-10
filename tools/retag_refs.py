"""Re-tag the photos the site can show with the two-step reference tagger (tools/tag_refs.py).

    python tools/retag_refs.py --limit 20   # try a few first
    python tools/retag_refs.py

Resume-safe: photos already done (marked "tagger": "refs-v1") are skipped. The previous render,
view and tags are kept as "prev" on each photo for comparison. Ends with the album rule.
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from common import ROOT, load_env, load_images, load_options, read_posts, save_images
from harmonize import harmonize
from tag import build_questions
from tag_refs import Usage, load_refs, tag_two_step

VERSION = "refs-v1"


def shown(r):
    pii = r.get("pii") or {}
    return (r.get("thumb") and r["view"] != "other" and (r.get("render") or 0) < 0.8
            and pii.get("face", 0) < 0.5 and pii.get("info", 0) < 0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Add OPENAI_API_KEY to .env.")

    doc, posts, options = load_images(), read_posts(), load_options()
    view_q, refs, usage = build_questions(options)[1], load_refs(), Usage()
    todo = [r for r in doc["images"] if r.get("tagger") != VERSION and r["post_id"] in posts
            and (shown(r) or r.get("prev"))]  # "prev": started under this version before
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} photos to re-tag")

    def run(r):
        img = Image.open(ROOT / r["thumb"]).convert("RGB")
        return tag_two_step(posts[r["post_id"]], img, options, view_q, refs, key, usage)

    failed = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(run, r): r for r in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                res = fut.result()
            except Exception as e:
                failed += 1
                print(f"  FAILED {r['thumb']}: {e}")
                if failed > 25 and failed > n * 0.05:
                    save_images(doc)
                    sys.exit(f"Stopping: {failed} failures in {n} photos. Saved progress; re-run to resume.")
                continue
            if "prev" not in r:
                old = {g: r["tags"].get(f"{g}_raw", r["tags"][g]) for g in ("paint", "interior", "wheels")}
                r["prev"] = {"render": r.get("render"), "view": r["view"], "tags": old}
            for g in ("paint", "interior", "wheels"):
                r["tags"].pop(f"{g}_raw", None)  # the album rule recomputes from the new tags
                r["tags"][g] = res["tags"][g]
            r.update(render=res["render"], view=res["view"], light=res["light"], tagger=VERSION)
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}  {usage}")
                save_images(doc)
    conflicts, changed, skipped = harmonize(doc)
    save_images(doc)
    print(f"Done: {len(todo) - failed} re-tagged, {failed} failed. Usage: {usage}")
    print(f"Album rule: disagreed {conflicts}, skipped {skipped}, changed {changed}")


if __name__ == "__main__":
    main()
