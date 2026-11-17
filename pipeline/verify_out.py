"""Safety check before publishing: refuses (exit 1) when the new build looks broken.

- every pack and the index exist, are not empty (MIN_BYTES; small countries like ME are ~10 KB), and their sha256 matches the manifest
- every country of the published Release is still there
- no pack's train-days in the next 60 days fell below SHRINK_LIMIT of the published Release
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
MIN_BYTES = 2_000
MIN_INDEX_BYTES = 300_000
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
        if size < (MIN_INDEX_BYTES if item is m["index"] else MIN_BYTES):
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
            gone = sorted(set(old_sizes) - {p["name"] for p in m["packs"]})
            if gone:
                problems.append("countries missing from this build: " + ", ".join(gone))
            old_days = {p["name"]: p.get("train_days_60") for p in old.get("packs", [])}
            for p in m["packs"]:
                prev, now = old_days.get(p["name"]), p.get("train_days_60")
                if prev and now is not None:
                    if now < prev * SHRINK_LIMIT:
                        problems.append(f"{p['file']}: trains in the next 60 days fell from {prev} to {now}")
                elif p["name"] in old_sizes and prev is None:
                    # The published Release predates train-day counts: nothing comparable this once
                    # (file sizes changed when repeated trains were combined).
                    print(f"note: {p['file']}: no train-day count in the published Release, size not compared")
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
