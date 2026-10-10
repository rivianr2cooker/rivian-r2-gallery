"""Add a lighting tag and re-ask the wheel question for the photos the site can show (one cheap call each).

    python tools/light_wheels.py --limit 20   # try a few first
    python tools/light_wheels.py

No reference photos. Interior photos only get the lighting question. Photos that already have
"light" are skipped, so re-running only does new ones. Ends by re-running the album rule.
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from common import ROOT, load_env, load_images, load_options, read_posts, save_images
from harmonize import harmonize
from tag import MAX_SIDE, context_text, to_jpeg
from tag_refs import LIGHT_Q, Usage, b64, call, group_questions, pick


def shown(r):
    pii = r.get("pii") or {}
    return (r.get("thumb") and r["view"] != "other" and (r.get("render") or 0) < 0.8
            and pii.get("face", 0) < 0.5 and pii.get("info", 0) < 0.5)


def check(rec, post, wheels_q, key, usage):
    img = Image.open(ROOT / rec["thumb"]).convert("RGB")
    questions = [LIGHT_Q] + ([wheels_q] if rec["view"] in ("exterior", "detail") else [])
    content = [{"type": "input_text", "text": context_text(post)},
               {"type": "input_image", "image_url": b64(to_jpeg(img, MAX_SIDE))}]
    a = call(content, questions, key, usage)
    return pick(a.get("light"))["value"], (pick(a.get("wheels")) if len(questions) > 1 else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Add OPENAI_API_KEY to .env.")

    doc, posts = load_images(), read_posts()
    wheels_q = group_questions(load_options())["wheels"]
    todo = [r for r in doc["images"] if shown(r) and "light" not in r and r["post_id"] in posts]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} photos to check")
    usage, failed, moved = Usage(), 0, 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(check, r, posts[r["post_id"]], wheels_q, key, usage): r for r in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                light, wheels = fut.result()
            except Exception as e:
                failed += 1
                print(f"  FAILED {r['thumb']}: {e}")
                continue
            r["light"] = light
            if wheels:
                r["tags"].pop("wheels_raw", None)  # the album rule recomputes from the new tag
                moved += wheels["value"] != r["tags"]["wheels"]["value"]
                r["tags"]["wheels"] = wheels
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}  {usage}")
                save_images(doc)
    conflicts, changed, skipped = harmonize(doc)
    save_images(doc)
    print(f"Done: {len(todo) - failed} checked, {failed} failed, {moved} wheel tags changed. Usage: {usage}")
    print(f"Album rule: disagreed {conflicts}, skipped {skipped}, changed {changed}")


if __name__ == "__main__":
    main()
