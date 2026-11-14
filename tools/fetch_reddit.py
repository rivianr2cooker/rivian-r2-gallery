"""Collect image posts from r/RivianR2 into data/raw/posts.jsonl, without Reddit's API.

Run by hand whenever you want new posts:
    python tools/fetch_reddit.py                 # posts since 2026-06-01
    python tools/fetch_reddit.py --since 2026-09-01
    python tools/fetch_reddit.py --since 2026-10-10 --source arcticshift   # from the Arctic Shift archive

--source arcticshift reads the same post data from Arctic Shift's public archive of Reddit
(arctic-shift.photon-reddit.com) instead of reddit.com: no cookie, no 1000-post cap (it pages
back by date), one request every 2-3 s. Use it when reddit.com blocks this connection. Posts
already in posts.jsonl are skipped. Photos are still downloaded from Reddit's image hosts when tagging.

Source: old.reddit.com's "new" listing plus a search per post flair (to reach past the
listing's ~1000-post limit), in JSON form, read with a logged-in session.
Reddit sends logged-out visitors to its login page, so put the `reddit_session`
cookie from a browser signed in to Reddit in .env as REDDIT_SESSION=... (use a
throwaway account). Requests are spaced 8 s apart (10 s for searches), with a longer pause when Reddit's rate-limit
headers run low.
Only posts that contain images are kept; text-only and video posts are dropped up front.
"""
import argparse
import html
import os
import random
import sys
import time
from datetime import datetime, timezone

import requests

from common import USER_AGENT, append_posts, load_env, read_posts

S = requests.Session()
S.headers["User-Agent"] = USER_AGENT
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")
PAUSE = 8          # seconds between listing requests (3 s got rate-limited after ~150 requests)
SEARCH_PAUSE = 10  # search is limited harder
JITTER = 1.5       # +/- seconds of randomness
ARCHIVE = "https://arctic-shift.photon-reddit.com/api/posts/search"
ARCHIVE_PAUSE = 2.5  # its docs allow a couple of requests per second; it's a free service, so stay well under


def images_from_json(d):
    """Image URLs for a post object: gallery or images embedded in the post text, single image,
    or preview fallback."""
    urls = []
    meta = d.get("media_metadata") or {}
    if meta:
        order = [i["media_id"] for i in (d.get("gallery_data") or {}).get("items", [])] or list(meta)
        for mid in order:
            m = meta.get(mid) or {}
            if m.get("e") == "Image" and m.get("s", {}).get("u"):
                urls.append(html.unescape(m["s"]["u"]))
    elif str(d.get("url", "")).split("?")[0].lower().endswith(IMG_EXT):
        urls.append(d["url"])
    elif not d.get("is_video") and (d.get("preview") or {}).get("images"):
        urls.append(html.unescape(d["preview"]["images"][0]["source"]["url"]))
    return urls


def post_record(d, imgs):
    author = d.get("author") or "[deleted]"
    return {
        "id": f"reddit:{d['id']}",
        "source": "reddit",
        "post_url": "https://www.reddit.com" + d["permalink"] if d.get("permalink") else f"https://www.reddit.com/comments/{d['id']}",
        "title": d.get("title", ""),
        "text": (d.get("selftext") or "")[:2000],
        "flair": d.get("link_flair_text"),
        "author": f"u/{author}",
        "author_url": None if author == "[deleted]" else f"https://www.reddit.com/user/{author}",
        "posted_at": int(d.get("created_utc") or 0),
        "images": imgs,
    }


def image_post(d):
    """Our post record for a Reddit post object, or None if it has no usable images."""
    if d.get("removed_by_category"):
        return None
    if d.get("crosspost_parent_list"):  # show the original post, credited to its poster
        d = {**d["crosspost_parent_list"][0], "created_utc": d.get("created_utc")}
    imgs = images_from_json(d)
    if imgs and not d.get("over_18") and not d.get("removed_by_category"):
        return post_record(d, imgs)
    return None


def reset_wait(r):
    """Seconds until Reddit's rate-limit window resets, from its response headers (capped at 10 min)."""
    for h in ("x-ratelimit-reset", "retry-after"):
        try:
            return min(float(r.headers[h]) + 5, 600)
        except (KeyError, ValueError):
            pass
    return 300


def get_page(url, params):
    pause = SEARCH_PAUSE if "/search" in url else PAUSE
    for attempt in range(3):
        r = S.get(url, params=params, timeout=30, allow_redirects=False)
        if r.is_redirect:
            raise requests.HTTPError("Reddit sent us to its login page: REDDIT_SESSION in .env is wrong or expired")
        if r.status_code == 429 and attempt < 2:  # wait out the window, at most twice
            wait = reset_wait(r)
            print(f"  rate limited; waiting {wait:.0f} s")
            time.sleep(wait)
            continue
        r.raise_for_status()  # 403, or a third 429: stop
        try:
            low = float(r.headers.get("x-ratelimit-remaining", "99")) < 5
        except ValueError:
            low = False
        time.sleep(reset_wait(r) if low else pause + random.uniform(-JITTER, JITTER))  # slow down before Reddit has to say so
        return r.json()["data"]


# Reddit listings stop after ~1000 posts (about three weeks here), so older posts are reached
# through flair searches, each of which has its own ~1000-post window.
FLAIRS = ["Reservation / Delivery / Sighting", "Discussion", "Question", "Accessories / Mods / Gear",
          "Feedback / Review", "Troubleshooting / Issue", "Granola Munching", "Tech & Software", "Funny",
          "Adventuring", "R2", "Feature Request", "Charging & Batteries", "Service", "Speculation"]


def read_listing(url, params, since_ts, seen, newest_first=True):
    """Image posts from one listing, until it runs out (or, if newest-first, reaches since_ts)."""
    posts, after = [], None
    while True:
        page = get_page(url, {**params, "limit": 100, "raw_json": 1, **({"after": after} if after else {})})
        for c in page["children"]:
            d = c["data"]
            if since_ts and int(d.get("created_utc") or 0) < since_ts:
                if newest_first:
                    return posts
                continue
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            p = image_post(d)
            if p:
                posts.append(p)
        after = page.get("after")
        if not after:
            return posts


def archive_page(params):
    """One page of Arctic Shift search results, honouring its rate-limit headers."""
    for attempt in range(5):
        r = S.get(ARCHIVE, params=params, timeout=60)
        if r.status_code in (422, 429, 500, 502, 503, 504) and attempt < 4:  # 422: its query timed out
            try:
                wait = min(float(r.headers["x-ratelimit-reset"]) + 2, 600) if r.status_code == 429 else 15 * (attempt + 1)
            except (KeyError, ValueError):
                wait = 60
            print(f"  archive returned {r.status_code}; waiting {wait:.0f} s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        try:
            low = float(r.headers.get("x-ratelimit-remaining", "99")) < 3
            wait = float(r.headers.get("x-ratelimit-reset", "60")) + 2 if low else ARCHIVE_PAUSE + random.uniform(-0.5, 0.5)
        except ValueError:
            wait = ARCHIVE_PAUSE
        time.sleep(min(wait, 600))
        return r.json().get("data") or []


def fetch_archive(sub, since_ts):
    """Image posts from Arctic Shift's archive of the subreddit, paging back by date from now to since_ts."""
    posts, seen, before, n = [], set(), None, 0
    while True:
        page = archive_page({"subreddit": sub, "after": since_ts, "limit": 100, "sort": "desc",
                             **({"before": before} if before else {})})
        fresh = [d for d in page if d["id"] not in seen]
        for d in fresh:
            seen.add(d["id"])
            p = image_post(d)
            if p:
                posts.append(p)
        n += 1
        if page:
            oldest = min(int(d["created_utc"]) for d in page)
            print(f"  page {n}: {len(page)} posts back to {datetime.fromtimestamp(oldest, timezone.utc):%Y-%m-%d %H:%M}, "
                  f"{len(posts)} image posts so far")
        if len(page) < 100 or not fresh:
            return posts
        before = oldest + 1  # +1: posts sharing the oldest second come back and are skipped as seen


# Keyword searches and other listings each have their own ~1000-post window too.
KEYWORDS = ["Esker Silver", "Glacier White", "Half Moon Grey", "Forest Green", "Midnight", "Catalina Cove",
            "Launch Green", "Borealis", "ES", "GW", "HMG", "FG", "CC", "LG",
            "Black Crater", "Coastal Cloud", "all terrain", "AT tires", "21", "Liquid Tungsten", "wheels", "tires",
            "delivery", "delivered", "picked up", "photos", "pics", "first drive", "road trip", "camping",
            "wrap", "PPF", "tint", "mods"]


def fetch_posts(sub, since_ts, start_at=None):
    base = f"https://old.reddit.com/r/{sub}"
    search = {"restrict_sr": 1, "include_over_18": "off"}
    listings = [("new", f"{base}/new.json", {}, True)]
    listings += [(f"flair {f}", f"{base}/search.json", {**search, "q": f'flair:"{f}"', "sort": "new"}, True) for f in FLAIRS]
    for k in KEYWORDS:
        listings += [(f"search {k} (new)", f"{base}/search.json", {**search, "q": f'"{k}"', "sort": "new"}, True),
                     (f"search {k} (top)", f"{base}/search.json", {**search, "q": f'"{k}"', "sort": "top", "t": "year"}, False)]
    for q in ("site:i.redd.it", "url:gallery", "self:yes"):  # image links, galleries, text posts (inline photos)
        listings += [(f"search {q}", f"{base}/search.json", {**search, "q": q, "sort": "new"}, True)]
    listings += [("hot", f"{base}/hot.json", {}, False), ("rising", f"{base}/rising.json", {}, False)]
    listings += [(f"top {t}", f"{base}/top.json", {"t": t}, False) for t in ("week", "month", "year", "all")]
    listings += [(f"controversial {t}", f"{base}/controversial.json", {"t": t}, False) for t in ("month", "year")]
    if start_at:  # resume a run that was stopped by rate limiting
        listings = listings[next(i for i, l in enumerate(listings) if l[0].startswith(start_at)):]
    posts, seen = [], set()
    for name, url, params, newest_first in listings:
        try:
            got = read_listing(url, params, since_ts, seen, newest_first)
        except requests.RequestException as e:
            print(f"  stopped early ({e}); keeping what was found")
            break
        posts += got
        print(f"  {name}: {len(got)} new image posts ({len(posts)} total)")
    return posts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subreddit", default="RivianR2")
    ap.add_argument("--since", default="2026-06-01", help="only posts on/after this date, YYYY-MM-DD")
    ap.add_argument("--from", dest="start_at", help='start at this listing, e.g. "search tires (top)" from a stopped run')
    ap.add_argument("--source", choices=("reddit", "arcticshift"), default="reddit",
                    help="reddit.com with a session cookie (default), or the Arctic Shift archive")
    args = ap.parse_args()
    since = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) if args.since else None

    if args.source == "arcticshift":
        if not since:
            sys.exit("--source arcticshift needs --since.")
        known = read_posts()
        found = fetch_archive(args.subreddit, since)
        posts = [p for p in found if p["id"] not in known]
        added = append_posts(posts)
        print(f"Found {len(found)} image posts, {len(found) - len(posts)} already collected, {added} new.")
        return

    load_env()
    cookie = os.environ.get("REDDIT_SESSION")
    if not cookie:
        sys.exit("Add REDDIT_SESSION=<reddit_session cookie> to the .env file next to README.md.")
    S.cookies.set("reddit_session", cookie, domain=".reddit.com")

    posts = fetch_posts(args.subreddit, since, args.start_at)
    added = append_posts(posts)
    print(f"Found {len(posts)} image posts, {added} new.")


if __name__ == "__main__":
    main()
