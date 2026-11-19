"""Start-of-session check: is there timetable data worth a new "Build packs" run?

Run by a Claude Code SessionStart hook (see ~/.claude/settings.json). Prints hook JSON only when
there is something to tell the user, so quiet sessions stay quiet.

Compares against the PUBLISHED Release (what the app has), not the local raw/ copies.
Checks (network results are cached for 6 hours in work/check_cache.json):
  1. Feeds whose server Last-Modified is newer than the Release's build time. Most sources republish
     daily, so this is only reported once the Release is STALE_DAYS old.
  2. Optional feeds now published but not in the Release; next year's Swiss dataset exists.
  3. The built packs run out within 21 days (local work/ packs) -> rebuild soon.

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
RELEASE_API = "https://api.github.com/repos/abhayclaude910-cyber/railplanner-data/releases/latest"
STALE_DAYS = 7
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


def published_release():
    """(tag, built datetime, feed ids in it) of the latest Release, or None when unreachable."""
    from datetime import datetime
    try:
        req = urllib.request.Request(RELEASE_API, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as res:
            rel = json.load(res)
        url = next(a["browser_download_url"] for a in rel["assets"] if a["name"] == "manifest.json")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=15) as res:
            m = json.load(res)
        built = datetime.fromisoformat(m["built"].replace("Z", "+00:00"))
        return m.get("tag") or rel["tag_name"], built, {c["feed"] for c in m.get("credits", [])}
    except Exception:
        return None


def network_findings():
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c["at"] < CACHE_HOURS * 3600:
                return c["findings"]
        except Exception:
            pass
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    findings = []
    with open(os.path.join(HERE, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    release = published_release()
    if release is None:
        return []  # offline or GitHub unreachable: say nothing rather than guess
    tag, built, in_release = release
    age = (datetime.now(timezone.utc) - built).days
    changed = []
    sys.path.insert(0, HERE)
    from download import resolve_url
    from concurrent.futures import ThreadPoolExecutor

    def probe(feed):  # all feeds at once: the hook has a 60 s limit
        try:
            return head(resolve_url(feed), feed.get("headers"))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(probe, feeds))
    for feed, now in zip(feeds, results):
        if now is None:
            continue
        if feed["id"] not in in_release:
            findings.append(f"{feed['id']} ({feed['name']}): available but not in Release {tag}")
            continue
        try:
            modified = parsedate_to_datetime(now[1]) if now[1] else None
        except Exception:
            modified = None
        if modified and modified > built:
            changed.append(feed["id"])
    if changed and age >= STALE_DAYS:
        findings.append(f"Release {tag} is {age} days old and {len(changed)} feeds have newer data "
                        f"({', '.join(changed)})")
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
    text = ("Rail planner data check (railplannerData, compared with the published Release): " + "; ".join(findings) +
            ". The user asked to be told about this at the start of every session: mention it briefly "
            "in your first reply and suggest starting the 'Build packs' workflow on GitHub (a local "
            "download/build does not reach the app). Otherwise the monthly build on the 1st covers it.")
    print(json.dumps({
        "systemMessage": "Rail planner: new timetable data to check (" + str(len(findings)) + ")",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text},
    }))


if __name__ == "__main__":
    main()
