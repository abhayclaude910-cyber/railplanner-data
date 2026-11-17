"""Downloads every feed in feeds.json into ../raw/<id>.zip.

Skips a feed when the server says the file has not changed since the last download
(compares Last-Modified / Content-Length kept in raw/<id>.json).

    python pipeline/download.py            # all feeds
    python pipeline/download.py de-fv nl   # only these

A feed that still fails after a few tries keeps its previous copy (raw/ is cached between
GitHub runs). Without a previous copy it is left out and build.py skips it; the run fails only
when a feed of a country already published fails (verify_out.py checks that) or nothing works.
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


def resolve_url(feed):
    """The feed's download link. Some sources publish a new dated file each week; look up the newest."""
    ds = feed.get("data_public_lu_dataset")
    if ds:
        api = f"https://data.public.lu/api/1/datasets/{ds}/"
        with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": UA}), timeout=60) as r:
            res = json.load(r)["resources"]
        zips = [x for x in res if x["url"].lower().endswith(".zip")]
        newest = max(zips, key=lambda x: x.get("last_modified") or "")
        return newest["url"]
    ds = feed.get("data_slovensko_dataset")
    if ds:  # Slovak open data portal: newest file of the dataset
        body = json.dumps({"language": "sk", "page": 1, "pageSize": 1, "filters": {"id": [ds]},
                           "requiredFacets": []}).encode()
        req = urllib.request.Request("https://data.slovensko.sk/datasets/search", data=body,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)["items"][0]["distributions"][0]["downloadUrl"]
    if "url_year" in feed:  # one file per timetable year ("..._2026.zip"); +1 = next year's, once published
        return feed["url"].replace("{year}", str(time.gmtime().tm_year + feed["url_year"]))
    return feed["url"]


def download(feed):
    os.makedirs(RAW, exist_ok=True)
    target = os.path.join(RAW, feed["id"] + ".zip")
    info_path = os.path.join(RAW, feed["id"] + ".json")
    old = {}
    if os.path.exists(info_path) and os.path.exists(target):
        with open(info_path, encoding="utf-8") as f:
            old = json.load(f)

    url = resolve_url(feed)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **feed.get("headers", {})})
    with urllib.request.urlopen(req, timeout=180) as res:
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
        json.dump({"url": url, "size": got, "last_modified": modified,
                   "downloaded": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, indent=1)
    print(f"{feed['id']}: done {got / 1e6:.1f} MB in {time.time() - start:.0f}s", flush=True)


errors = {}


def download_retry(feed, tries=3):
    for attempt in range(1, tries + 1):
        try:
            download(feed)
            return True
        except Exception as e:
            print(f"{feed['id']}: try {attempt} failed: {e!r}", flush=True)
            errors[feed["id"]] = repr(e)[:200]
            part = os.path.join(RAW, feed["id"] + ".zip.part")
            if os.path.exists(part):
                os.remove(part)
            if attempt < tries:
                time.sleep(20 * attempt)
    return False


def main():
    wanted = set(sys.argv[1:])
    failed, kept = [], []
    for feed in load_feeds():
        if wanted and feed["id"] not in wanted:
            continue
        if not download_retry(feed, tries=1 if feed.get("optional") else 3):
            if feed.get("optional"):
                print(f"{feed['id']}: not published (yet), skipped")
                errors.pop(feed["id"], None)
                continue
            if os.path.exists(os.path.join(RAW, feed["id"] + ".zip")):
                kept.append(feed["id"])
            else:
                failed.append(feed["id"])
    if os.environ.get("GITHUB_ACTIONS"):  # annotations are readable on the run page without logs
        for x in kept:
            print(f"::warning::{x}: download failed, built from the previous copy ({errors[x]})")
        for x in failed:
            print(f"::warning::{x}: download failed, left out ({errors[x]})")
    if kept:
        print("WARNING download failed, using the previous copy: " + ", ".join(kept))
    if failed:
        print("WARNING download failed, left out of this build: " + ", ".join(failed))
    with open(os.path.join(RAW, "download_report.json"), "w", encoding="utf-8") as f:
        json.dump({"kept_old": kept, "missing": failed}, f)
    if not any(os.path.exists(os.path.join(RAW, x["id"] + ".zip")) for x in load_feeds()):
        sys.exit("no feed could be downloaded")


if __name__ == "__main__":
    main()
