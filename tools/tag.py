"""Tag collected images with OpenAI's Decisions API and write data/images.json.

    # put OPENAI_API_KEY=sk-... in .env next to README.md (or set it in your shell)
    python tools/tag.py            # tags every image not tagged yet
    python tools/tag.py --limit 20 # try a few first

Images already in data/images.json are skipped, so re-running only pays for new ones.
The Decisions API only accepts inline base64 images, so each image is downloaded
and shrunk before sending.
"""
import argparse
import base64
import io
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from PIL import Image

from harmonize import harmonize
from common import THUMBS, load_env, USER_AGENT, load_images, load_options, read_posts, save_images

API_URL = "https://api.openai.com/v1/decisions"
MODEL = "gpt-6-luna"
MAX_SIDE = 1024   # px sent to the model
KEEP_SIDE = 1024  # px of the copy stored in the repo and shown on the site

S = requests.Session()
S.headers["User-Agent"] = USER_AGENT


SHORTHAND = (" Owners often write their config as paint/interior/wheels shorthand separated by slashes or commas, "
             "e.g. 'ES/CC/AT' = Esker Silver, Coastal Cloud interior, All-Terrain wheels, or 'LG, BCS, 21\" LTS' = Launch "
             "Green, Black Crater, 21\" Liquid Tungsten; abbreviations are listed with each option. 'CC' in the paint "
             "slot is Catalina Cove, in the interior slot Coastal Cloud. Use this when the photo agrees or can't settle it.")


def aka(o):
    return f' (owners write: {", ".join(o["aliases"])})' if o.get("aliases") else ""


RENDER_Q = {"type": "predicate", "name": "render",
            "instructions": "Is this NOT a real owner photo of a real car? Answer true for screenshots of the Rivian "
                            "configurator, app or website, official press or marketing images, computer renders, and "
                            "studio shots of a car on a plain seamless background. Answer false for real photos taken "
                            "by people, even of a screen-free car at a dealership or delivery center."}


def build_questions(options):
    def choice(name, instructions, opts, fallback):
        return {
            "type": "choice",
            "name": name,
            "instructions": instructions + SHORTHAND,
            "choices": [{"value": o["value"], "description": f'{o["label"]}{aka(o)}: {o["description"]}'} for o in opts]
                       + [{"value": "unknown", "description": fallback}],
        }

    return [
        RENDER_Q,
        {"type": "choice", "name": "view",
         "instructions": "What does the photo mainly show? Every post is from an R2 owner community, so assume any "
                         "Rivian in it is an R2, unless the post compares it with another Rivian (R1S, R1T, R3) and "
                         "this photo shows that other model: then answer other.",
         "choices": [
             {"value": "exterior", "description": "The outside of the vehicle."},
             {"value": "interior", "description": "The cabin: seats, dashboard, trim."},
             {"value": "detail", "description": "A close-up of one part, like a wheel, badge or light."},
             {"value": "other", "description": "Anything else: no car, screenshots of Rivian's configurator or app, "
                                                 "press/marketing renders, studio shots on plain backgrounds, CGI, memes, documents."},
         ]},
        choice("paint", "Which factory paint color is the R2 in the photo? Judge from the photo; "
                        "the post text may name the color and can break ties. Lighting shifts colors, "
                        "so compare against all options.",
               options["paint"], "Exterior paint not visible, wrapped, or can't tell."),
        choice("interior", "Which interior color scheme is visible in the photo?",
               options["interior"], "Interior not visible or can't tell."),
        choice("wheels", "Which wheels and tires is the R2 in the photo fitted with? Only launch-config factory wheels "
                         "are listed; a factory wheel not in the list counts as unknown.",
               options["wheels"], "Wheels not visible or can't tell."),
    ]


def fetch_image(url):
    r = S.get(url, timeout=60)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    img = img.convert("RGB")
    return img


def to_jpeg(img, side, quality=85):
    img = img.copy()
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def context_text(post):
    where = "r/RivianR2 on Reddit" if post["source"] == "reddit" else "the R2 section of rivianforums.com"
    parts = [f"Posted in {where}.", f"Post title: {post['title']}"]
    if post.get("flair"):
        parts.append(f"Flair: {post['flair']}")
    if post.get("text"):
        parts.append(f"Post text: {post['text'][:1500]}")
    if post.get("op_comments"):
        parts.append("Poster's own comments: " + " | ".join(post["op_comments"])[:1500])
    if len(post["images"]) > 1:
        parts.append(f"This is one of {len(post['images'])} photos in the post; the text may describe several cars.")
    if post["source"] == "forum":
        parts.append("Forum replies often quote other people's cars; judge this photo by itself.")
    return "\n".join(parts)


def decide(questions, post, jpeg, key):
    body = {
        "model": MODEL,
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": context_text(post)},
                {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()},
            ],
        }],
        "questions": questions,
    }
    for attempt in range(5):
        r = requests.post(API_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=120)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"Decisions API {r.status_code}: {r.text[:300]}")
        return {a["name"]: a for a in r.json()["answers"]}
    raise RuntimeError("Decisions API kept failing")


def summarize(answers):
    def pick(name):
        a = answers.get(name, {})
        if a.get("type") != "choice":
            return {"value": "unknown", "confidence": 0}
        return {"value": a["choice"], "confidence": round(a.get("confidence", 0), 3)}

    r = answers.get("render", {})
    return {
        "render": round(r.get("probability", 0), 3) if r.get("type") == "predicate" else None,
        "view": pick("view")["value"],
        "tags": {k: pick(k) for k in ("paint", "interior", "wheels")},
    }


def process(post, idx, url, questions, key):
    img = fetch_image(url)
    result = summarize(decide(questions, post, to_jpeg(img, MAX_SIDE), key))
    thumb = None
    if result["view"] != "other":  # only keep copies of photos that might be shown
        THUMBS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{post['id']}_{idx}") + ".jpg"
        (THUMBS / name).write_bytes(to_jpeg(img, KEEP_SIDE, 80))
        thumb = f"assets/photos/{name}"
    return {
        "id": f"{post['id']}:{idx}",
        "post_id": post["id"],
        "source": post["source"],
        "post_url": post["post_url"],
        "image_url": url,
        "thumb": thumb,
        "author": post["author"],
        "author_url": post.get("author_url"),
        "title": post["title"],
        "posted_at": post.get("posted_at", 0),
        **result,
    }


def check_render(rec, post, key):
    """Ask only the render question for an image tagged before it existed (uses the local copy)."""
    path = THUMBS.parent.parent / rec["thumb"] if rec.get("thumb") else None
    img = Image.open(path).convert("RGB") if path and path.exists() else fetch_image(rec["image_url"])
    a = decide([RENDER_Q], post, to_jpeg(img, MAX_SIDE), key).get("render", {})
    return round(a.get("probability", 0), 3)


def check_renders(doc, key, workers):
    posts = read_posts()
    todo = [r for r in doc["images"] if r.get("render") is None and r["view"] != "other" and r["post_id"] in posts]
    print(f"{len(todo)} shown-or-showable images to check for configurator/press/render shots")
    flagged = failed = 0
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(check_render, r, posts[r["post_id"]], key): r for r in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            r = futs[fut]
            try:
                r["render"] = fut.result()
                flagged += r["render"] >= 0.5
            except Exception as e:
                failed += 1
                print(f"  FAILED {r['image_url']}: {e}")
            if n % 50 == 0:
                print(f"  {n}/{len(todo)} checked, {flagged} flagged")
                save_images(doc)
    save_images(doc)
    print(f"Checked {len(todo) - failed}, flagged {flagged} as not real photos, failed {failed}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="tag at most this many images")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--retag", action="store_true", help="re-tag images already in images.json")
    ap.add_argument("--check-renders", action="store_true",
                    help="only ask the render question for images tagged before it was added")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("Add OPENAI_API_KEY=sk-... to the .env file next to README.md.")

    questions = build_questions(load_options())
    doc = load_images()
    if args.check_renders:
        return check_renders(doc, key, args.workers)
    done = set() if args.retag else {(i["id"], i["image_url"]) for i in doc["images"]}
    todo = [(p, i, u) for p in read_posts().values() if p["source"] in ("reddit", "forum")
            for i, u in enumerate(p["images"]) if (f"{p['id']}:{i}", u) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} images to tag")

    by_id = {i["id"]: i for i in doc["images"]}
    ok = failed = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(process, p, i, u, questions, key): (p, u) for p, i, u in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                rec = fut.result()
                by_id[rec["id"]] = rec
                ok += 1
                t = rec["tags"]
                print(f"[{n}/{len(todo)}] {rec['view']:8} {t['paint']['value']}/{t['interior']['value']}/{t['wheels']['value']}  {rec['image_url']}")
            except Exception as e:  # keep going; the image is retried next run
                failed += 1
                print(f"[{n}/{len(todo)}] FAILED {futs[fut][1]}: {e}")
            if n % 25 == 0:
                doc["images"] = sorted(by_id.values(), key=lambda r: -r["posted_at"])
                save_images(doc)

    doc["images"] = sorted(by_id.values(), key=lambda r: -r["posted_at"])
    doc["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conflicts, changed, skipped = harmonize(doc)
    save_images(doc)
    print(f"Tagged {ok}, failed {failed}. Wrote data/images.json ({len(doc['images'])} images).")
    print(f"Albums that disagreed: {conflicts}, skipped as multi-car: {skipped}, photos changed: {changed}")


if __name__ == "__main__":
    main()
