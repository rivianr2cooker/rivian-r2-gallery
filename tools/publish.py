"""Upload the site, data and photos to GitHub without git or the GitHub CLI.

Uses GitHub's REST API with a fine-grained personal access token that only has
access to this one repository, so nothing else on your machine or account changes.

One-time setup:
  1. github.com → Settings → Developer settings → Fine-grained tokens → Generate new token
     Repository access: Only select repositories → rivian-r2-gallery
     Permissions: Contents → Read and write
  2. Create a file named .env next to README.md containing:
        GITHUB_TOKEN=github_pat_...
        GITHUB_REPO=rivianr2cooker/rivian-r2-gallery

Then, after fetching and tagging:
    python tools/publish.py

Only files that changed are uploaded, in a single commit.
"""
import base64
import hashlib
import os
import sys
import time

import requests

from common import ROOT, load_env, load_images

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
SKIP_FILES = {".env"}
SKIP_PATHS = ("data/raw/", "references/")  # post text and tagger reference photos: not used by the site
SKIP_SUFFIXES = (".log", ".pyc")
API = "https://api.github.com"


def shown_photos():
    """Photo copies the site can display: same rules as inMode() in app.js (any source, 60% tags)."""
    def known(r, g):
        t = r["tags"].get(g, {})
        return t.get("value", "unknown") != "unknown" and t.get("confidence", 0) >= 0.6
    keep = set()
    for r in load_images()["images"]:
        pii = r.get("pii") or {}
        if (not r.get("thumb") or r["view"] == "other" or (r.get("render") or 0) >= 0.8
                or pii.get("face", 0) >= 0.5 or pii.get("info", 0) >= 0.5):
            continue
        if (r["view"] != "interior" and known(r, "paint")) or (r["view"] != "exterior" and known(r, "interior")):
            keep.add(r["thumb"])
    return keep


def local_files():
    photos = shown_photos()
    out = {}
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if (p.is_file() and not SKIP_DIRS.intersection(rel.parts) and p.name not in SKIP_FILES
                and not rel.as_posix().startswith(SKIP_PATHS) and not p.name.endswith(SKIP_SUFFIXES)
                and (not rel.as_posix().startswith("assets/photos/") or rel.as_posix() in photos)):
            out[rel.as_posix()] = p
    return out


def git_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main():
    load_env()
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo:
        sys.exit("Set GITHUB_TOKEN and GITHUB_REPO in .env (see the top of this file).")
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def call(method, path, **kw):
        r = s.request(method, f"{API}/repos/{repo}{path}", timeout=120, **kw)
        if not r.ok:
            sys.exit(f"GitHub {method} {path} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    # Commit as the account's GitHub noreply address, never its real email (commits on a public repo are public).
    me = s.get(f"{API}/users/{repo.split('/')[0]}", timeout=30).json()
    ident = {"name": me["login"], "email": f'{me["id"]}+{me["login"]}@users.noreply.github.com'}

    info = call("GET", "")
    branch = info["default_branch"]
    ref = s.get(f"{API}/repos/{repo}/git/ref/heads/{branch}", timeout=60)
    if ref.status_code in (404, 409):  # empty repo: the Git Data API needs one commit to exist
        readme = (ROOT / "README.md").read_bytes()
        call("PUT", "/contents/README.md", json={"message": "Initial commit", "branch": branch,
                                                 "author": ident, "committer": ident,
                                                 "content": base64.b64encode(readme).decode()})
        ref = s.get(f"{API}/repos/{repo}/git/ref/heads/{branch}", timeout=60)
    head = ref.json()["object"]["sha"]
    base_tree = call("GET", f"/git/commits/{head}")["tree"]["sha"]
    remote = {e["path"]: e["sha"] for e in call("GET", f"/git/trees/{base_tree}?recursive=1")["tree"] if e["type"] == "blob"}

    files = local_files()
    changed = [(path, p.read_bytes()) for path, p in sorted(files.items()) if remote.get(path) != git_sha(p.read_bytes())]
    if not changed:
        print("Nothing changed.")
        return
    print(f"Uploading {len(changed)} changed files...")

    def upload(path, data):
        time.sleep(1)  # GitHub's secondary rate limit trips on rapid content creation
        return call("POST", "/git/blobs", json={"content": base64.b64encode(data).decode(), "encoding": "base64"})["sha"]

    tree, uploaded = base_tree, 0
    for i in range(0, len(changed), 200):  # GitHub 502s on one huge tree request; build it up in steps
        chunk = changed[i:i + 200]
        # Blobs from an earlier, interrupted run are already on GitHub: try their known hashes first.
        r = s.post(f"{API}/repos/{repo}/git/trees", timeout=120, json={"base_tree": tree, "tree": [
            {"path": path, "mode": "100644", "type": "blob", "sha": git_sha(data)} for path, data in chunk]})
        if r.status_code == 422:  # some blob is missing: upload this chunk
            entries = []
            for path, data in chunk:
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": upload(path, data)})
                uploaded += 1
            r = s.post(f"{API}/repos/{repo}/git/trees", timeout=120, json={"base_tree": tree, "tree": entries})
        if not r.ok:
            sys.exit(f"GitHub POST /git/trees failed: {r.status_code} {r.text[:300]}")
        tree = r.json()["sha"]
        print(f"  {min(i + 200, len(changed))}/{len(changed)} files in the commit ({uploaded} uploaded)")
    commit = call("POST", "/git/commits", json={"message": f"Update site ({len(changed)} files)", "tree": tree, "parents": [head],
                                               "author": ident, "committer": ident})
    call("PATCH", f"/git/refs/heads/{branch}", json={"sha": commit["sha"]})
    print(f"Pushed {len(changed)} files to {repo}@{branch}. GitHub Pages will redeploy in a minute or two.")


if __name__ == "__main__":
    main()
