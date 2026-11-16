"""Start-of-session check: is there newer timetable data than the last download/build?

Run by a Claude Code SessionStart hook (see ~/.claude/settings.json). Prints hook JSON only when
there is something to tell the user, so quiet sessions stay quiet.

Checks (network results are cached for 6 hours in work/check_cache.json):
  1. Each feed in feeds.json: server Last-Modified / size differs from raw/<id>.json -> new data.
  2. Next year's Swiss dataset (timetable-<year+1>-gtfs2020) exists -> feeds.json URL must change.
  3. The built packs run out within 21 days (manifest day0 + last service date) -> rebuild soon.

    python pipeline/check_new_data.py          # hook mode (JSON)
    python pipeline/check_new_data.py --text   # plain text, for a person
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "raw")
WORK = os.path.join(ROOT, "work")
CACHE = os.path.join(WORK, "check_cache.json")
CACHE_HOURS = 6
UA = "railplanner-data-pipeline/0.1 (+https://github.com/abhayclaude910-cyber/railplanner-data)"
SWISS_NEXT = "https://data.opentransportdata.swiss/dataset/timetable-{year}-gtfs2020/permalink"


def head(url, extra=None):
    """(size, last_modified) from the server, or None when unreachable. Uses a 1-byte GET (some hosts reject HEAD)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0", **(extra or {})})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            cr = res.headers.get("Content-Range") or ""
            size = int(cr.split("/")[-1]) if "/" in cr else int(res.headers.get("Content-Length") or 0)
            return size, res.headers.get("Last-Modified") or ""
    except Exception:
        return None


def exists(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as res:
            return res.status in (200, 206)
    except Exception:
        return False


def network_findings():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c["at"] < CACHE_HOURS * 3600:
                return c["findings"]
        except Exception:
            pass
    findings = []
    with open(os.path.join(HERE, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    for feed in feeds:
        info_path = os.path.join(RAW, feed["id"] + ".json")
        old = {}
        if os.path.exists(info_path):
            with open(info_path, encoding="utf-8") as f:
                old = json.load(f)
        now = head(feed["url"], feed.get("headers"))
        if now is None:
            continue
        size, modified = now
        if not old:
            findings.append(f"{feed['id']} ({feed['name']}): never downloaded")
        elif (modified and modified != old.get("last_modified")) or (size and size != old.get("size")):
            findings.append(f"{feed['id']} ({feed['name']}): NEW DATA on the server "
                            f"({modified or 'new size'}; last download {old.get('downloaded', '?')[:10]})")
    # Next timetable year for Switzerland (its URL contains the year).
    # Swiss feeds have the year in the URL: tell when next year's dataset exists but is not listed.
    if any(f["country"] == "CH" for f in feeds):
        year = date.today().year + (1 if date.today().month >= 9 else 0)
        listed = any(f"timetable-{year}-" in f["url"] for f in feeds)
        if not listed and exists(SWISS_NEXT.format(year=year)):
            findings.append(f"CH: the {year} Swiss timetable is published; add a 'ch-{year}' feed to feeds.json "
                            f"(URL timetable-{year}-gtfs2020) and rebuild; drop the oldest Swiss feed once its year has ended")
    os.makedirs(WORK, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump({"at": time.time(), "findings": findings}, f)
    return findings


def expiry_findings():
    """Warn when the built packs stop covering future dates soon."""
    import sqlite3
    out = []
    for name in ("de", "ch", "nl"):
        path = os.path.join(WORK, f"{name}.sqlite")
        if not os.path.exists(path):
            continue
        try:
            db = sqlite3.connect(path)
            day0 = date.fromisoformat(db.execute("SELECT value FROM meta WHERE key='day0'").fetchone()[0])
            last = -1
            for (m,) in db.execute("SELECT days FROM services"):
                for i in range(len(m) - 1, -1, -1):
                    if m[i]:
                        last = max(last, i * 8 + m[i].bit_length() - 1)
                        break
            db.close()
        except Exception:
            continue
        if last < 0:
            continue
        end = date.fromordinal(day0.toordinal() + last)
        left = (end - date.today()).days
        if left <= 21:
            out.append(f"{name.upper()} pack: timetable data ends {end.isoformat()} ({max(left, 0)} days left), "
                       f"rebuild with the new data")
    return out


def main():
    try:
        findings = network_findings() + expiry_findings()
    except Exception as e:  # never break a session start
        findings = [f"check failed: {e}"]
    if "--text" in sys.argv:
        print("\n".join(findings) or "No new rail timetable data.")
        return
    if not findings:
        return
    text = ("Rail planner data check (railplannerData): " + "; ".join(findings) +
            ". The user asked to be told about this at the start of every session: mention it briefly "
            "in your first reply and offer to download and rebuild "
            "(python pipeline/download.py, then python pipeline/build.py in D:/Projects/ClaudeProject/railplannerData).")
    print(json.dumps({
        "systemMessage": "Rail planner: new timetable data to check (" + str(len(findings)) + ")",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
    }))


if __name__ == "__main__":
    main()
