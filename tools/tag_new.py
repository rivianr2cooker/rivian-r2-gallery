"""Tag new photos with the two-step reference tagger (tools/tag_refs.py) and add them to data/images.json.

    python tools/tag_new.py --since 2026-10-10 --limit 20   # try a few first
    python tools/tag_new.py --since 2026-10-10

Only images that aren't in images.json yet are tagged (Reddit posts on/after --since by default).
Each one is downloaded once, then gets render, view, light and paint/interior/wheels tags in one go
(marked "tagger": "refs-v1", like tools/retag_refs.py). A 1024px copy is kept unless it's "other".
Token usage is printed and appended to data/raw/tag_usage.log. Ends with the album rule.
Run tools/pii_check.py afterwards so the new photos get their face/info check.
"""
import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from common import DATA, THUMBS, load_env, load_images, load_options, read_posts, save_images
from harmonize import harmonize
from tag import KEEP_SIDE, build_questions, fetch_image, to_jpeg
from tag_refs import Usage, load_refs, tag_two_step

VERSION = "refs-v1"


def process(post, idx, url, options, view_q, refs, key, usage):
    try:
        img = fetch_image(url)
    except Exception as e:
        raise LookupError(f"download failed: {e}") from e
    res = tag_two_step(post, img, options, view_q, refs, key, usage)
    thumb = None
    if res["view"] != "other":  # only keep copies of photos that might be shown
        THUMBS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{post['id']}_{idx}") + ".jpg"
        (THUMBS / name).write_bytes(to_jpeg(img, KEEP_SIDE, 80))
        thumb = f"assets/photos/{name}"
    return {
        "id": f"{post['id']}:{idx}", "post_id": post["id"], "source": post["source"], "post_url": post["post_url"],
        "image_url": url, "thumb": thumb, "author": post["author"], "author_url": post.get("author_url"),
        "title": post["title"], "posted_at": post.get("posted_at", 0),
        "render": res["render"], "view": res["view"], "light": res["light"], "tags": res["tags"], "tagger": VERSION,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="only posts on/after this date, YYYY-MM-DD")
    ap.add_argument("--source", default="reddit", choices=("reddit", "forum"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY") or sys.exit("Add OPENAI_API_KEY to .env.")
    since = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    doc, options = load_images(), load_options()
    view_q, refs, usage = build_questions(options)[1], load_refs(), Usage()
    done = {(i["id"], i["image_url"]) for i in doc["images"]}
    todo = [(p, i, u) for p in read_posts().values() if p["source"] == args.source and p.get("posted_at", 0) >= since
            for i, u in enumerate(p["images"]) if (f"{p['id']}:{i}", u) not in done]
    todo.sort(key=lambda t: -t[0].get("posted_at", 0))
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(todo)} images to tag")

    by_id = {i["id"]: i for i in doc["images"]}
    ok = dl_failed = api_failed = 0

    def save():
        doc["images"] = sorted(by_id.values(), key=lambda r: -r["posted_at"])
        save_images(doc)

    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(process, p, i, u, options, view_q, refs, key, usage): (p, i, u) for p, i, u in todo}
        for n, fut in enumerate(as_completed(futs), 1):
            p, i, u = futs[fut]
            try:
                rec = fut.result()
            except LookupError as e:  # deleted or unreachable image: retried next run
                dl_failed += 1
                print(f"[{n}/{len(todo)}] SKIPPED {p['id']}:{i}: {e}")
                continue
            except Exception as e:
                api_failed += 1
                print(f"[{n}/{len(todo)}] FAILED {p['id']}:{i}: {e}")
                if api_failed > 10 and api_failed > n * 0.05:
                    for f in futs:
                        f.cancel()
                    save()
                    sys.exit(f"Stopping: {api_failed} tagging failures in {n} images. Saved progress; re-run to resume.")
                continue
            by_id[rec["id"]] = rec
            ok += 1
            t = rec["tags"]
            print(f"[{n}/{len(todo)}] {rec['view']:8} r={rec['render']:.2f} {rec['light']:4} "
                  f"{t['paint']['value']}/{t['interior']['value']}/{t['wheels']['value']}")
            if n % 50 == 0:
                print(f"  usage so far: {usage}")
                save()

    doc["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conflicts, changed, skipped = harmonize(doc)
    save()
    print(f"Tagged {ok}, download failed {dl_failed}, tagging failed {api_failed}. images.json now has {len(doc['images'])} images.")
    print(f"Usage: {usage}")
    print(f"Album rule: disagreed {conflicts}, skipped {skipped}, changed {changed}")
    with (DATA / "raw" / "tag_usage.log").open("a", encoding="utf-8") as f:
        f.write(f"{doc['generated']} tag_new --since {args.since} --source {args.source}: tagged {ok}, "
                f"download failed {dl_failed}, tagging failed {api_failed}; calls {usage.calls}, input {usage.input}, "
                f"cached {usage.cached}, output {usage.output}\n")


if __name__ == "__main__":
    main()
