"""Downloads every feed in feeds.json into ../raw/<id>.zip.

Skips a feed when the server says the file has not changed since the last download
(compares Last-Modified / Content-Length kept in raw/<id>.json).

    python pipeline/download.py            # all feeds
    python pipeline/download.py de-fv nl   # only these
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "raw")
UA = "railplanner-data-pipeline/0.1 (+https://github.com/abhayclaude910-cyber/railplanner-data)"


def load_feeds():
    with open(os.path.join(HERE, "feeds.json"), encoding="utf-8") as f:
        return json.load(f)["feeds"]


def download(feed):
    os.makedirs(RAW, exist_ok=True)
    target = os.path.join(RAW, feed["id"] + ".zip")
    info_path = os.path.join(RAW, feed["id"] + ".json")
    old = {}
    if os.path.exists(info_path) and os.path.exists(target):
        with open(info_path, encoding="utf-8") as f:
            old = json.load(f)

    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA, **feed.get("headers", {})})
    with urllib.request.urlopen(req, timeout=60) as res:
        size = int(res.headers.get("Content-Length") or 0)
        modified = res.headers.get("Last-Modified") or ""
        if old and old.get("last_modified") == modified and old.get("size") == size and modified:
            print(f"{feed['id']}: unchanged ({size / 1e6:.1f} MB), skipped")
            return
        tmp = target + ".part"
        got = 0
        start = time.time()
        last = 0.0
        if (res.headers.get("Content-Encoding") or "").lower() == "gzip":
            import gzip as _gz
            res = _gz.GzipFile(fileobj=res)
            size = 0  # length is of the compressed body
        with open(tmp, "wb") as out:
            while True:
                chunk = res.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if time.time() - last > 5:
                    last = time.time()
                    pct = f"{got * 100 / size:.0f}%" if size else ""
                    print(f"{feed['id']}: {got / 1e6:.0f} MB {pct}", flush=True)
    if size and got != size:
        raise RuntimeError(f"{feed['id']}: got {got} bytes, expected {size}")
    os.replace(tmp, target)
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump({"url": feed["url"], "size": got, "last_modified": modified,
                   "downloaded": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1)
    print(f"{feed['id']}: done {got / 1e6:.1f} MB in {time.time() - start:.0f}s", flush=True)


def main():
    wanted = set(sys.argv[1:])
    for feed in load_feeds():
        if wanted and feed["id"] not in wanted:
            continue
        download(feed)


if __name__ == "__main__":
    main()
