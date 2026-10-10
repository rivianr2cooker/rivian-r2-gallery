"""Make tags consistent within an album: every photo in a multi-image post is treated as the same car.

    python tools/harmonize.py   # also runs automatically at the end of tools/tag.py

Per post, for paint (exterior/detail photos) and interior (interior photos): among photos whose
own tag is known and at least MIN_TAG confident, the value on the most photos wins; a tie goes
to the tied value with the single highest confidence. Those photos get the winning value, with
the highest confidence any photo gave it. A post is left alone when a losing value is on 3+
photos, or on 2+ photos making up a third of the voters, since that usually means several cars. The photo's own tag is kept in tags.<group>_raw, so
re-running always starts from the original tags. Unknown or low-confidence tags are left alone.
Wheels are not harmonized.
"""
from collections import defaultdict

from common import load_images, save_images

MIN_TAG = 0.6  # same as the site's threshold (MIN_TAG in app.js)
GROUPS = {"paint": ("exterior", "detail"), "interior": ("interior",)}


def harmonize(doc):
    """Returns (albums with a conflict, photos changed, albums skipped as multi-car), per group."""
    posts = defaultdict(list)
    for img in doc["images"]:
        posts[img["post_id"]].append(img)
    conflicts, changed, skipped = defaultdict(int), defaultdict(int), defaultdict(int)
    for imgs in posts.values():
        for group, views in GROUPS.items():
            for img in imgs:  # start from each photo's own tag
                t = img["tags"]
                if f"{group}_raw" in t:
                    t[group] = dict(t.pop(f"{group}_raw"))
            voters = [i for i in imgs if i["view"] in views and (i.get("render") or 0) < 0.8
                      and i["tags"][group]["value"] != "unknown" and i["tags"][group]["confidence"] >= MIN_TAG]
            values = {i["tags"][group]["value"] for i in voters}
            if len(values) < 2:
                continue
            conflicts[group] += 1
            count = lambda v: sum(i["tags"][group]["value"] == v for i in voters)
            best = lambda v: max(i["tags"][group]["confidence"] for i in voters if i["tags"][group]["value"] == v)
            winner = max(values, key=lambda v: (count(v), best(v)))
            if any(count(v) >= 3 or (count(v) >= 2 and count(v) * 3 >= len(voters)) for v in values - {winner}):
                skipped[group] += 1  # a strong second color: probably several cars, keep per-photo tags
                continue
            for i in voters:
                t = i["tags"]
                t[f"{group}_raw"] = dict(t[group])
                if t[group]["value"] != winner:
                    changed[group] += 1
                t[group] = {"value": winner, "confidence": best(winner), "album": True}
    return dict(conflicts), dict(changed), dict(skipped)


def main():
    doc = load_images()
    conflicts, changed, skipped = harmonize(doc)
    save_images(doc)
    for g in GROUPS:
        print(f"{g}: {conflicts.get(g, 0)} albums disagreed, {skipped.get(g, 0)} skipped as multi-car, "
              f"{changed.get(g, 0)} photos changed")


if __name__ == "__main__":
    main()
