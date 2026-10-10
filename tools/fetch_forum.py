"""Collect image posts from XenForo forums (e.g. rivianforums.com) into data/raw/posts.jsonl.

Run by hand. With no arguments it reads the Rivian R2 forum on rivianforums.com
and its subforums (newest threads first):
    python tools/fetch_forum.py --pages 2
    python tools/fetch_forum.py https://www.rivianforums.com/forum/threads/<thread>/   # specific threads only

Each forum *post* that contains images becomes one entry, credited to its author.
Note: written against standard XenForo 2 markup; not yet tested against the live site.
"""
import argparse
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from common import USER_AGENT, append_posts

S = requests.Session()
S.headers["User-Agent"] = USER_AGENT
SKIP_IMG = re.compile(r"(smilies|emoji|avatar|data/assets|/styles/|reaction)", re.I)


class Blocked(Exception):
    """The site rate-limited (429) or refused (403) us: stop the run."""


def get(url):
    r = S.get(url, timeout=30)
    time.sleep(random.uniform(3, 5))
    if r.status_code in (403, 429):
        raise Blocked(f"{r.status_code} for {url}")
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


R2_FORUM = "https://www.rivianforums.com/forum/forums/rivian-r2-forum.8/"
FORUMS = {  # short names for --forum
    "rivianforums": R2_FORUM,
    "rivianownersforum": "https://www.rivianownersforum.com/forums/rivian-r2-discussions.69/",
}


def subforums(forum_url):
    soup = get(forum_url)
    subs = [urljoin(forum_url, a["href"]) for a in soup.select(".node-title a[href*='/forums/']")]
    return [forum_url] + list(dict.fromkeys(subs))


def thread_urls(forum_url, pages, since_ts=None):
    """Thread links from a forum listing, skipping threads with no posts since since_ts."""
    urls = []
    for n in range(1, pages + 1):
        page = forum_url if n == 1 else urljoin(forum_url, f"page-{n}")
        soup = get(page)
        for item in soup.select(".structItem--thread"):
            a = item.select_one(".structItem-title a[href*='/threads/']")
            latest = item.select_one(".structItem-cell--latest time[data-time]")
            if not a or (since_ts and latest and int(latest["data-time"]) < since_ts):
                continue
            u = urljoin(page, a["href"]).split("#")[0]
            if "/unread" not in u and u not in urls:
                urls.append(u)
    return urls


def posts_in_thread(url, max_pages):
    out = []
    soup = get(url)
    title_el = soup.select_one("h1.p-title-value")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    last = max([int(a.get_text()) for a in soup.select(".pageNav-page a") if a.get_text().isdigit()] or [1])
    host = urlparse(url).netloc.replace(".", "_")
    for n in range(max(1, last - max_pages + 1), last + 1):  # newest pages: that's where recent photos are
        if n > 1:
            soup = get(urljoin(url.rstrip("/") + "/", f"page-{n}"))
        for art in soup.select("article.message[data-author]"):
            body = art.select_one(".message-body .bbWrapper")
            if not body:
                continue
            imgs = []
            for a in body.select("a[href*='/attachments/']"):
                imgs.append(urljoin(url, a["href"]))
            for img in body.select("img"):
                src = img.get("data-url") or img.get("data-src") or img.get("src") or ""
                if src and not src.startswith("data:") and not SKIP_IMG.search(src):
                    imgs.append(urljoin(url, src))
            imgs = list(dict.fromkeys(imgs))
            if not imgs:
                continue
            post_id = art.get("data-content", "").replace("post-", "") or art.get("id", "")
            author = art["data-author"]
            prof = art.select_one("a.username[href]")
            t = art.select_one("time[data-time]")
            for q in body.select("blockquote"):
                q.decompose()  # don't let quoted text describe this post's car
            out.append({
                "id": f"forum:{host}:{post_id}",
                "source": "forum",
                "post_url": permalink(art, url, post_id),
                "title": title,
                "text": body.get_text(" ", strip=True)[:2000],
                "author": author,
                "author_url": urljoin(url, prof["href"]) if prof else None,
                "posted_at": int(t["data-time"]) if t else 0,
                "images": imgs,
            })
    return out


def permalink(art, thread_url, post_id):
    """The post's own link, as the site writes it (forums differ in their URL prefix)."""
    for a in art.select("a[href]"):
        if re.search(rf"/posts/{post_id}/?$|/post-{post_id}$", a["href"]):
            return urljoin(thread_url, a["href"])
    return f"{thread_url}#post-{post_id}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("threads", nargs="*", help="thread URLs")
    ap.add_argument("--forum", default="rivianforums",
                    help=f"forum listing URL, or one of: {', '.join(FORUMS)}")
    ap.add_argument("--since", default="2026-06-01", help="skip threads with no posts since YYYY-MM-DD")
    ap.add_argument("--no-subforums", action="store_true", help="don't also read the forum's subforums")
    ap.add_argument("--pages", type=int, default=1, help="listing pages to read with --forum")
    ap.add_argument("--thread-pages", type=int, default=5, help="max pages per thread (the newest ones)")
    args = ap.parse_args()
    forum = FORUMS.get(args.forum, args.forum)
    since = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    urls, posts = list(args.threads), []
    try:
        if not urls:
            for f in [forum] if args.no_subforums else subforums(forum):
                urls += [u for u in thread_urls(f, args.pages, since) if u not in urls]
            print(f"{len(urls)} threads with posts since {args.since}")
        for n, u in enumerate(urls, 1):
            try:
                posts += [p for p in posts_in_thread(u, args.thread_pages) if p["posted_at"] >= since]
            except requests.HTTPError as e:
                print(f"skip {u}: {e}")
            if n % 25 == 0:
                print(f"  {n}/{len(urls)} threads, {len(posts)} image posts")
    except Blocked as e:
        print(f"Stopped early ({e}); keeping what was found.")
    added = append_posts(posts)
    print(f"Read {len(urls)} threads, {len(posts)} image posts, {added} new.")


if __name__ == "__main__":
    main()
