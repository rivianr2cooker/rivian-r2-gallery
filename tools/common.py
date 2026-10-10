"""Shared helpers for the fetch and tag scripts."""
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW_POSTS = DATA / "raw" / "posts.jsonl"
IMAGES_JSON = DATA / "images.json"
OPTIONS_JSON = DATA / "options.json"
THUMBS = ROOT / "assets" / "photos"

USER_AGENT = "r2-gallery/0.1 (manual, low-volume fan site scraper)"


def load_options():
    return json.loads(OPTIONS_JSON.read_text(encoding="utf-8"))


def read_posts():
    """Return {post_id: post} from the raw posts file (later lines win)."""
    posts = {}
    if RAW_POSTS.exists():
        for line in RAW_POSTS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = json.loads(line)
                posts[p["id"]] = p
    return posts


def append_posts(new_posts):
    """Append posts not already present. Returns how many were added."""
    existing = read_posts()
    RAW_POSTS.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with RAW_POSTS.open("a", encoding="utf-8") as f:
        for p in new_posts:
            if not p["images"] or existing.get(p["id"], {}).get("images") == p["images"]:
                continue  # read_posts keeps the last line, so a changed post just gets re-appended
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
            existing[p["id"]] = p
            added += 1
    return added


def load_images():
    if IMAGES_JSON.exists():
        return json.loads(IMAGES_JSON.read_text(encoding="utf-8"))
    return {"generated": None, "images": []}


def save_images(doc):
    IMAGES_JSON.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def load_env():
    """Read KEY=value lines from .env next to README.md into the environment."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
