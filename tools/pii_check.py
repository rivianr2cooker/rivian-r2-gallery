"""Flag personal information visible inside the photos the site can show.

    python tools/pii_check.py        # checks photos not checked yet, using the local copies

Asks three yes/no questions per photo and stores the probabilities in images.json as
"pii": {"plate", "face", "info"}. It only records them; hiding or blurring is a separate step.
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

from common import DATA, ROOT, load_env, load_images, read_posts, save_images
from tag import MAX_SIDE, context_text, to_jpeg
from tag_refs import Usage, b64, call

QUESTIONS = [
    {"type": "predicate", "name": "plate",
     "instructions": "Is a vehicle license plate legible in this photo, so its characters could be read?"},
    {"type": "predicate", "name": "face",
     "instructions": "Is a person's face clearly visible and identifiable in this photo (not tiny, blurred, "
                     "turned away or covered)?"},
    {"type": "predicate", "name": "info",
     "instructions": "Is personal information legible in this photo: a VIN, a window sticker or Monroney label, "
                     "a document, receipt, order or delivery paperwork, or a screen showing a name, address, "
                     "phone number, email, VIN or order number?"},
]


def shown(rec):
    return rec["view"] != "other" and (rec.get("render") or 0) < 0.8 and rec.get("thumb")


def check(rec, post, key, usage):
    img = Image.open(ROOT / rec["thumb"]).convert("RGB")
    content = [{"type": "input_text", "text": context_text(post)},
               {"type": "input_image", "image_url": b64(to_jpeg(img, MAX_SIDE))}]
    answers = call(content, QUESTIONS, key, usage)
    return {q["name"]: round(answers.get(q["name"], {}).get("probability", 0), 3) for q in QUESTIONS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Add OPENAI_API_KEY to .env.")

    doc, posts = load_images(), read_posts()
    todo = [r for r in doc["images"] if shown(r) and "pii" not in r and r["post_id"] in posts]
    print(f"{len(todo)} photos to check")
    failed, usage = 0, Usage()
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(check, r, posts[r["post_id"]], key, usage): r for r in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                r["pii"] = fut.result()
            except Exception as e:
                failed += 1
                print(f"  FAILED {r['thumb']}: {e}")
            if n % 100 == 0:
                print(f"  {n}/{len(todo)}")
                save_images(doc)
    save_images(doc)
    checked = [r for r in doc["images"] if "pii" in r]
    for q in QUESTIONS:
        print(f"{q['name']}: {sum(r['pii'][q['name']] >= 0.5 for r in checked)} of {len(checked)} photos")
    print(f"failed {failed}. Usage: {usage}")
    if todo:
        with (DATA / "raw" / "tag_usage.log").open("a", encoding="utf-8") as f:
            f.write(f"pii_check: checked {len(todo) - failed}, failed {failed}; calls {usage.calls}, input {usage.input}, "
                    f"cached {usage.cached}, output {usage.output}\n")


if __name__ == "__main__":
    main()
