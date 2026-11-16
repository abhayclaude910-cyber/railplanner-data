"""Safety check before publishing: refuses (exit 1) when the new build looks broken.

- every pack and the index exist, are at least MIN_BYTES, and their sha256 matches the manifest
- no pack shrank below SHRINK_LIMIT of the same pack in the currently published Release
  (catches a feed that came back empty or half-downloaded)

    python pipeline/verify_out.py
"""
import hashlib
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "out")
MIN_BYTES = 300_000
SHRINK_LIMIT = 0.6
LATEST = "https://github.com/{repo}/releases/latest/download/manifest.json"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    with open(os.path.join(OUT, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    problems = []
    for item in m["packs"] + [m["index"]]:
        path = os.path.join(OUT, item["file"])
        if not os.path.exists(path):
            problems.append(f"{item['file']} missing")
            continue
        size = os.path.getsize(path)
        if size < MIN_BYTES:
            problems.append(f"{item['file']} only {size} bytes")
        if sha256(path) != item["sha256"]:
            problems.append(f"{item['file']} sha256 does not match the manifest")

    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        try:
            req = urllib.request.Request(LATEST.format(repo=repo), headers={"User-Agent": "railplanner-data-ci"})
            with urllib.request.urlopen(req, timeout=20) as res:
                old = json.load(res)
            old_sizes = {p["name"]: p["size"] for p in old.get("packs", [])}
            for p in m["packs"]:
                prev = old_sizes.get(p["name"])
                if prev and p["size"] < prev * SHRINK_LIMIT:
                    problems.append(f"{p['file']} shrank from {prev / 1e6:.1f} MB to {p['size'] / 1e6:.1f} MB")
        except Exception as e:  # first Release, or GitHub unreachable: size checks above still apply
            print(f"note: no previous Release to compare with ({e})")

    for p in m["packs"] + [m["index"]]:
        print(f"{p['file']}: {p['size'] / 1e6:.2f} MB, {p['count']} items")
    if problems:
        print("REFUSING TO PUBLISH:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("ok")


if __name__ == "__main__":
    main()
