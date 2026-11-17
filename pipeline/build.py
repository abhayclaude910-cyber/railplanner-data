"""Builds the offline packs from the raw GTFS zips.

    python pipeline/build.py

Output (in ../out):
    <cc>.sqlite.gz          one pack per country in PACK_COUNTRIES
    stations_index.sqlite.gz all stations with search, shipped inside the app
    manifest.json            versions, sizes, sha256, credits
    report.txt               counts and warnings

Pack format: see ../FORMAT.md.
"""
import gzip
import hashlib
import json
import math
import os
import re
import statistics
import shutil
import sqlite3
import sys
import time
import unicodedata
from array import array
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtfs import Feed, kind_of, parse_time, service_days  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "raw")
OUT = os.path.join(ROOT, "out")
WORK = os.path.join(ROOT, "work")

FORMAT_VERSION = 1
NDAYS = 400                 # service days kept, starting at the build date
DEFAULT_TRANSFER = 300      # seconds, when the feed gives none
MERGE_ANY_NAME_M = 120      # stations from different feeds closer than this are the same station
MERGE_SIMILAR_NAME_M = 600  # ...or closer than this when the names look alike
MERGE_SAME_SOURCE_M = 300   # two entries of one source: same station only if this close and alike
# Stations outside this box have broken coordinates (feeds.json can set "valid_box" per feed,
# e.g. for Crete or Cyprus later). The Dutch feed puts some ferry stops in Algeria.
EUROPE_BOX = (34.5, -10.6, 71.5, 45.0)
KIND_CODE = {"rail": 0, "ferry": 1, "coach": 2}

# UIC country codes (first two digits of a 7-digit station number).
UIC_COUNTRY = {
    "10": "FI", "44": "BA", "50": "BA", "51": "PL", "52": "BG", "53": "RO", "54": "CZ", "55": "HU",
    "56": "SK", "60": "IE", "62": "ME", "65": "MK", "70": "GB", "71": "ES", "72": "RS", "73": "GR",
    "74": "SE", "75": "TR", "76": "NO", "78": "HR", "79": "SI", "80": "DE", "81": "AT", "82": "LU",
    "83": "IT", "84": "NL", "85": "CH", "86": "DK", "87": "FR", "88": "BE", "94": "PT",
}
# Rough boxes (lat1, lon1, lat2, lon2) used only to spot stations that are clearly abroad.
BOX = {
    "DE": (47.2, 5.8, 55.1, 15.1), "CH": (45.8, 5.9, 47.85, 10.5), "NL": (50.7, 3.3, 53.6, 7.25),
    "FR": (41.3, -5.2, 51.1, 9.6), "ES": (35.9, -9.4, 43.8, 4.4), "FI": (59.7, 20.5, 70.1, 31.6),
    "NO": (57.9, 4.5, 71.2, 31.1), "BE": (49.4, 2.5, 51.6, 6.5), "IE": (51.3, -10.7, 55.5, -5.4),
    "LU": (49.4, 5.7, 50.2, 6.6), "PT": (36.9, -9.6, 42.2, -6.1), "EE": (57.5, 21.7, 59.8, 28.3),
    "RO": (43.6, 20.2, 48.3, 29.8), "LV": (55.6, 20.9, 58.1, 28.3), "ME": (41.8, 18.4, 43.6, 20.4),
    "PL": (49.0, 14.1, 54.9, 24.2), "BG": (41.2, 22.3, 44.3, 28.7),
    "AT": (46.3, 9.4, 49.1, 17.3), "SK": (47.7, 16.8, 49.65, 22.6), "DK": (54.5, 8.0, 57.8, 15.2),
    "CZ": (48.5, 12.0, 51.1, 18.9), "SI": (45.4, 13.3, 46.9, 16.65), "LT": (53.8, 20.9, 56.5, 26.9),
    "GR": (34.8, 19.3, 41.8, 29.7), "RS": (42.2, 18.8, 46.2, 23.1), "TR": (35.8, 25.6, 42.2, 44.9),
    "MK": (40.8, 20.4, 42.4, 23.1), "SE": (55.3, 10.9, 69.1, 24.2),
}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_feeds():
    with open(os.path.join(HERE, "feeds.json"), encoding="utf-8") as f:
        return json.load(f)["feeds"]


# ---------------------------------------------------------------- reading one feed

class FeedData:
    """What we keep from one GTFS feed, with feed-local ids."""

    def __init__(self, cfg, day0):
        self.cfg = cfg
        self.id = cfg["id"]
        self.space = cfg.get("id_space", cfg["id"])
        self.country = cfg["country"]
        path = os.path.join(RAW, self.id + ".zip")
        feed = Feed(path)
        t0 = time.time()

        agencies = {r.get("agency_id", ""): r.get("agency_name", "") for r in feed.rows("agency.txt")}

        # Routes we keep.
        self.routes = {}
        for r in feed.rows("routes.txt"):
            try:
                rt = int(r["route_type"])
            except ValueError:
                continue
            kind = kind_of(rt)
            if not kind:
                continue
            color = (r.get("route_color") or "").strip()
            short = (r.get("route_short_name") or "").strip()
            # Internal line codes ("601A" in the SNCF feed) mean nothing to travellers.
            agency_name = agencies.get(r.get("agency_id", ""), "")
            if re.fullmatch(r"\d{3}[A-Z]", short) and (
                    cfg.get("code_route_label") or re.search(r"SNCF|Chemins de fer Fran", agency_name)):
                short = cfg.get("code_route_label") or "SNCF"
            long_name = (r.get("route_long_name") or "").strip()
            # Line codes in front of the train type ("A3 RJX", "R19 R", "SI1 D") mean little to travellers.
            if cfg.get("route_short_strip"):
                short = re.sub(cfg["route_short_strip"], "", short).strip() or short
            # Line numbers as names ("425", "43"): show the service or the operator instead
            # ("Pendeltåg", "SJ", "Öresundståg"), with the train number after it.
            if cfg.get("numeric_short_label") and short.isdigit():
                for name in (long_name, agency_name):
                    if name and len(name) <= 16:
                        short = name
                        break
            # Train types written in the local alphabet ("БВ") get an English word from feeds.json.
            label = cfg.get("route_labels", {}).get(short)
            if label:
                short, long_name = label
            if not short and kind == "rail":
                # Nothing to show before the train number ("6152"): name the operator instead.
                short = cfg.get("default_label") or (agency_name if 0 < len(agency_name) <= 12 else "")
            self.routes[r["route_id"]] = {
                "kind": kind, "type": rt,
                "short": latin_name(short, cfg["country"]),
                "long": latin_name(long_name, cfg["country"]),
                "agency": agencies.get(r.get("agency_id", ""), next(iter(agencies.values()), "")),
                "color": int(color, 16) if re.fullmatch(r"[0-9A-Fa-f]{6}", color) else None,
            }

        # Trips we keep.
        self.trips = {}
        services = set()
        # Some feeds (BDŽ) name the train type in the trip's route_id ("KPV_82201", "IVKONI-PV_80107")
        # and do not always list those ids in routes.txt: build one route per train type instead.
        pattern = cfg.get("trip_route_pattern")
        if pattern:
            labels = cfg.get("route_labels", {})
            first_agency = next(iter(agencies.values()), "")
            self.routes = {}
        for r in feed.rows("trips.txt"):
            if pattern:
                m = re.match(pattern, r["route_id"])
                code = m.group("type") if m else ""
                if code not in labels:  # e.g. "A" = replacement bus, like route_type 3 elsewhere
                    continue
                agency = (m.group("agency") or "").title() or first_agency
                rid = f"{agency}:{code}"
                if rid not in self.routes:
                    self.routes[rid] = {"kind": "rail", "type": 2, "short": labels[code][0], "long": labels[code][1],
                                        "agency": agency, "color": None}
                r = dict(r, route_id=rid)
            if r["route_id"] not in self.routes:
                continue
            headsign = (r.get("trip_headsign") or "").strip()
            number = (r.get("trip_short_name") or "").strip()
            if number.isdigit():
                number = number.lstrip("0") or number  # "000031" -> "31"
            if not number and headsign.isdigit():  # SNCF puts the train number in the headsign
                number, headsign = headsign, ""
            # Line code as route name and the train type in the number ("A3" + "RJX 19962", "R19" + "R 865"):
            # one route per line and type, named after the type, so the label reads "RJX 19962".
            m = cfg.get("type_in_number") and re.fullmatch(r"([A-Za-z]{1,4})\s*(\d+)", number)
            if m:
                typed = f"{r['route_id']}|{m.group(1)}"
                if typed not in self.routes:
                    self.routes[typed] = dict(self.routes[r["route_id"]], short=m.group(1))
                r = dict(r, route_id=typed)
                number = m.group(2)
            self.trips[r["trip_id"]] = {
                "route": r["route_id"], "service": r["service_id"],
                "headsign": latin_name(fix_caps(headsign, cfg), cfg["country"]),
                "number": number,
                "bikes": (r.get("bikes_allowed") or "").strip(),
                "rt": (r.get("realtime_trip_id") or r.get("original_trip_id") or "").strip(),
            }
            services.add(r["service_id"])
        # Service days first: trips that never run in our window are dropped before the (huge)
        # stop_times read, which keeps memory down when two timetable years are loaded.
        masks = service_days(feed, day0, NDAYS, wanted=services)
        self.masks = {s: bytes(masks.get(s, b"")) for s in services}
        before = len(self.trips)
        self.trips = {k: v for k, v in self.trips.items() if any(self.masks[v["service"]])}
        log(f"{self.id}: {len(self.routes)} routes, {len(self.trips)} trips kept "
            f"({before - len(self.trips)} not running in the next {NDAYS} days)")

        # Stops -> stations.
        raw_stops = {}
        for r in feed.rows("stops.txt"):
            try:
                lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
            except (ValueError, KeyError):
                lat = lon = None
            raw_stops[r["stop_id"]] = (
                fix_caps((r.get("stop_name") or "").strip(), cfg), lat, lon,
                (r.get("parent_station") or "").strip(), (r.get("platform_code") or "").strip(),
                (r.get("didok") or "").strip(),
            )

        # Stop times for kept trips: per trip an array of (seq, stop index, arr, dep, flags).
        self.stop_index = {}   # stop_id -> local index
        self.stop_ids = []
        times = {}
        col, reader = feed.table("stop_times.txt")
        i_trip, i_seq, i_stop = col["trip_id"], col["stop_sequence"], col["stop_id"]
        i_arr, i_dep = col["arrival_time"], col["departure_time"]
        i_pick, i_drop = col.get("pickup_type"), col.get("drop_off_type")
        i_head = col.get("stop_headsign")
        last_head = {}
        n = 0
        trips = self.trips
        for row in reader:
            n += 1
            if n % 5_000_000 == 0:
                log(f"{self.id}: stop_times {n // 1_000_000}M rows")
            tid = row[i_trip]
            if tid not in trips:
                continue
            sid = row[i_stop]
            si = self.stop_index.get(sid)
            if si is None:
                si = len(self.stop_ids)
                self.stop_index[sid] = si
                self.stop_ids.append(sid)
            arr = parse_time(row[i_arr])
            dep = parse_time(row[i_dep])
            flags = 0
            if i_pick is not None and row[i_pick].strip() == "1":
                flags |= 1
            if i_drop is not None and row[i_drop].strip() == "1":
                flags |= 2
            a = times.get(tid)
            if a is None:
                a = times[tid] = array("i")
            a.extend((int(row[i_seq]), si, -1 if arr is None else arr, -1 if dep is None else dep, flags))
            if i_head is not None and row[i_head]:
                last_head[tid] = row[i_head].strip()
        log(f"{self.id}: read {n} stop_times rows")

        # Sort each trip by sequence, fill missing times, drop trips with < 2 stops.
        self.trip_stops = {}
        for tid, a in times.items():
            rows = sorted((tuple(a[k:k + 5]) for k in range(0, len(a), 5)), key=lambda x: x[0])
            if len(rows) < 2:
                continue
            filled = fill_times([[r[1], r[2], r[3], r[4]] for r in rows])
            if filled is None:
                continue
            self.trip_stops[tid] = filled
            if not trips[tid]["headsign"] and tid in last_head:
                trips[tid]["headsign"] = last_head[tid]
        del times

        # Frequencies: copy trips at each headway.
        self.freq_copies = 0
        for r in feed.rows("frequencies.txt"):
            tid = r["trip_id"]
            if tid not in self.trip_stops:
                continue
            start, end, step = parse_time(r["start_time"]), parse_time(r["end_time"]), int(r["headway_secs"])
            base = self.trip_stops[tid]
            shift0 = start - base[0][2]
            k = 0
            t = start
            while t < end and step > 0:
                new_id = f"{tid}#f{k}"
                sh = shift0 + k * step
                self.trip_stops[new_id] = [[s, ar + sh, dp + sh, fl] for s, ar, dp, fl in base]
                trips[new_id] = dict(trips[tid])
                k += 1
                t += step
            if k:
                del self.trip_stops[tid]
                self.freq_copies += k

        # Stations: parent station when there is one, else the stop itself.
        self.stations = {}     # station key -> {name, lat, lon, uic}
        self.stop_station = []  # local stop index -> (station key, platform)
        box = cfg.get("valid_box", EUROPE_BOX)

        def valid(la, lo):
            return la is not None and lo is not None and km_outside(la, lo, box) == 0

        for sid in self.stop_ids:
            name, lat, lon, parent, platform, didok = raw_stops.get(sid, ("", None, None, "", "", ""))
            if not valid(lat, lon):
                lat = lon = None
            key = sid
            if parent:
                # Use the parent id even when this file lacks the parent row, so feeds that share
                # ids (gtfs.de long distance + regional) end up on the same station.
                key = parent
                if parent in raw_stops:
                    pname, plat_, plon, _, _, pdidok = raw_stops[parent]
                    # The parent's position wins, unless it is broken (the Dutch feed has parent rows
                    # at 0,0 or in Africa while their platforms are right): then keep the platform's.
                    if valid(plat_, plon):
                        lat, lon = plat_, plon
                    # Parent name wins ("München Hbf" over "München Hbf Gl.5-10"), unless it is a
                    # local name unlike the stop's ("Ostbahnhof" vs "München Ost").
                    child = strip_platform(name)
                    if pname and (names_alike(pname, child) or not child):
                        name = pname
                    else:
                        name = child or pname
                    didok = didok or pdidok
            have = self.stations.get(key)
            if have is None:
                self.stations[key] = {"name": name, "lat": lat or None, "lon": lon or None,
                                      "uic": uic_of(didok) or uic_of(key, cfg.get("uic_from_id", ""))}
            elif not have["lat"] and lat:
                have["lat"], have["lon"] = lat, lon
            self.stop_station.append((key, platform))

        # Broken coordinates (e.g. Dutch ferry stops placed in Algeria): mark, trips using them are dropped.
        self.bad_stations = []
        for key, info in self.stations.items():
            if not valid(info["lat"], info["lon"]):
                info["bad"] = True
                self.bad_stations.append(info["name"])

        # Minimum transfer time per station (same stop -> same stop rows only).
        self.transfer = {}
        col, reader = feed.table("transfers.txt")
        if col:
            f_from, f_to, f_min = col.get("from_stop_id"), col.get("to_stop_id"), col.get("min_transfer_time")
            f_ft, f_tt = col.get("from_trip_id"), col.get("to_trip_id")
            f_fr, f_tr = col.get("from_route_id"), col.get("to_route_id")
            for row in reader:
                if f_min is None or row[f_from] != row[f_to] or not row[f_min]:
                    continue
                if any(i is not None and row[i] for i in (f_ft, f_tt, f_fr, f_tr)):
                    continue
                sid = row[f_from]
                key = sid
                if sid in raw_stops and raw_stops[sid][3]:
                    key = raw_stops[sid][3]
                self.transfer[key] = max(self.transfer.get(key, 0), int(float(row[f_min])))

        # Service days.
        log(f"{self.id}: {len(self.trip_stops)} trips with stops, {len(self.stations)} stations, "
            f"{self.freq_copies} frequency copies, {time.time() - t0:.0f}s")


def km_outside(lat, lon, box):
    """Rough distance in km from a point to a lat/lon box (0 inside)."""
    dlat = max(box[0] - lat, 0, lat - box[2]) * 111
    dlon = max(box[1] - lon, 0, lon - box[3]) * 111 * math.cos(math.radians(lat))
    return math.hypot(dlat, dlon)


def uic_of(code, rule=""):
    """7-digit UIC number from a station code, or ''.
    rule (feeds.json "uic_from_id"): "padded" = zero-padded number ("000005457076" -> 5457076);
    "czptt" = Czech id "czptt:stop:CZ:57076" -> 5457076."""
    code = code or ""
    if rule == "padded" and code.isdigit():
        n = code.lstrip("0")
        return n if len(n) == 7 and n[:2] in UIC_COUNTRY else ""
    if rule == "czptt":
        m = re.fullmatch(r"czptt:stop:CZ:(\d{5})", code)
        return "54" + m.group(1) if m else ""
    m = re.search(r"(?<!\d)(\d{7})(?!\d)", code)
    return m.group(1) if m else ""


def fill_times(stops):
    """stops: [stop, arr, dep, flags] with -1 for missing times. Fills gaps; None if unusable."""
    for s in stops:
        if s[1] < 0 and s[2] >= 0:
            s[1] = s[2]
        if s[2] < 0 and s[1] >= 0:
            s[2] = s[1]
    known = [i for i, s in enumerate(stops) if s[1] >= 0]
    if len(known) < 2 or known[0] != 0 or known[-1] != len(stops) - 1:
        return None
    for a, b in zip(known, known[1:]):
        for i in range(a + 1, b):
            t = stops[a][2] + (stops[b][1] - stops[a][2]) * (i - a) // (b - a)
            stops[i][1] = stops[i][2] = t
    for i in range(len(stops)):  # never go back in time
        if stops[i][1] < stops[i - 1][2] and i > 0:
            stops[i][1] = stops[i - 1][2]
        if stops[i][2] < stops[i][1]:
            stops[i][2] = stops[i][1]
    return stops


# ---------------------------------------------------------------- station merging

def norm_name(s):
    s = latinize(s) or s  # Greek / Cyrillic names compare in Latin letters
    s = s.replace("ß", "ss").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)", " ", s)
    # Czech / Slovak abbreviations: "Praha hl.n." = "Praha hlavní nádraží", "hl.st." = "hlavná stanica"
    s = re.sub(r"(?<![a-z])hl\.?\s*n(?![a-z])\.?", " hlavni nadrazi ", s)
    s = re.sub(r"(?<![a-z])hl\.?\s*st(?![a-z])\.?", " hlavna stanica ", s)
    s = s.replace("hauptbahnhof", "hbf").replace("centraal", "cs").replace("central", "cs")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.split()


with open(os.path.join(HERE, "pass_rules.json"), encoding="utf-8") as _f:
    PASS_RULES = json.load(_f)


def pass_code(route):
    """0 = normal, 1 = rail passes usually not valid, 2 = pass discount only (see pass_rules.json)."""
    agency = (route.get("agency") or "").lower()
    short = route.get("short") or ""
    nv = PASS_RULES["not_valid"]
    if any(a in agency for a in nv["agency_contains"]) or any(short.startswith(p) for p in nv["route_short_prefix"]):
        return 1
    if any(a in agency for a in PASS_RULES["discount_only"]["agency_contains"]):
        return 2
    return 0


def tidy_agency(s):
    """'800151 DB Regio AG Nordost' -> 'DB Regio AG Nordost' (gtfs.de prefixes some names with a code)."""
    return re.sub(r"^[0-9A-Z]{6}\s+(?=\D)", "", (s or "").strip())


def strip_platform(s):
    """'München Hbf Gl.5-10' / 'Köln Hbf (tief)' / 'Bern, Gleis 3' -> station name only."""
    s = re.sub(r"[ ,]+(Gl\.|Gleis|Bahnsteig|Spoor|Perron)\s*\S+.*$", "", s)
    s = re.sub(r"\s*\((tief|oben|unten)\)", "", s)
    return s.strip()


def tidy_name(s):
    """'Mannheim, Hauptbahnhof' / 'Hannover Hauptbahnhof' -> 'Mannheim Hbf' / 'Hannover Hbf'."""
    s = re.sub(r",?\s+Hauptbahnhof\b", " Hbf", s)
    m = re.match(r"^([^,]+),\s*(.+)$", s)
    if m and m.group(2).lower().startswith(m.group(1).lower()):   # "Hamburg, Hamburg Hbf"
        s = m.group(2)
    return re.sub(r"\s{2,}", " ", s).strip()


def country_by_box(lat, lon):
    """Rough guess for stations near Germany/Switzerland/Netherlands that have no UIC number."""
    hits = [(abs((b[2] - b[0]) * (b[3] - b[1])), cc) for cc, b in ROUGH_BOX.items()
            if b[0] <= lat <= b[2] and b[1] <= lon <= b[3]]
    return min(hits)[1] if hits else "??"


ROUGH_BOX = {
    "DE": (47.2, 5.8, 55.1, 15.1), "CH": (45.8, 5.9, 47.85, 10.5), "NL": (50.7, 3.3, 53.6, 7.25),
    "SE": (55.3, 10.9, 69.1, 24.2), "GB": (49.9, -8.2, 58.7, 1.8), "ES": (35.9, -9.4, 43.8, 4.4),
    "FI": (59.7, 20.5, 70.1, 31.6), "RU": (54.0, 28.0, 70.0, 60.0), "IE": (51.3, -10.7, 55.5, -5.4),
    "PT": (36.9, -9.6, 42.2, -6.1), "EE": (57.5, 21.7, 59.8, 28.3), "LV": (55.6, 20.9, 58.1, 28.3),
    "LT": (53.8, 20.9, 56.5, 26.9), "RO": (43.6, 20.2, 48.3, 29.8), "BG": (41.2, 22.3, 44.3, 28.7),
    "ME": (41.8, 18.4, 43.6, 20.4), "RS": (42.2, 18.8, 46.2, 23.1), "GR": (34.8, 19.3, 41.8, 29.7),
    "TR": (35.8, 25.6, 42.2, 44.9), "MK": (40.8, 20.4, 42.4, 23.1), "BA": (42.5, 15.7, 45.3, 19.7),
    "UA": (44.3, 22.1, 52.4, 40.3), "BY": (51.2, 23.1, 56.2, 32.8), "MD": (45.4, 26.6, 48.5, 30.2), "NO": (57.9, 4.5, 71.2, 31.1),
    "AT": (46.4, 9.5, 49.0, 17.2), "CZ": (48.55, 12.1, 51.06, 18.9), "BE": (49.5, 2.5, 51.5, 6.4),
    "LU": (49.45, 5.73, 50.18, 6.53), "FR": (42.3, -4.8, 51.1, 8.2), "PL": (49.0, 14.1, 54.9, 24.2),
    "DK": (54.55, 8.0, 57.8, 15.2), "IT": (36.6, 6.6, 47.1, 18.5), "SK": (47.7, 16.8, 49.6, 22.6),
    "HU": (45.7, 16.1, 48.6, 22.9), "SI": (45.4, 13.4, 46.9, 16.6),
}


CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
    "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz", "ѓ": "gj", "ќ": "kj", "ѕ": "dz",
    "ё": "e", "ы": "y", "э": "e", "і": "i", "ї": "yi", "є": "ye",
}


# Serbian and Macedonian write Cyrillic in Latin letters differently from Bulgarian.
CYRILLIC_LANG = {
    "SR": {"ц": "c", "ч": "č", "ш": "š", "ж": "ž", "ђ": "đ", "ћ": "ć", "џ": "dž", "х": "h", "й": "j"},
    "MK": {"ц": "c", "ч": "č", "ш": "š", "ж": "ž", "џ": "dž", "х": "h", "ѓ": "gj", "ќ": "kj", "ѕ": "dz"},
}
CYRILLIC_LANG["RS"] = CYRILLIC_LANG["ME"] = CYRILLIC_LANG["BA"] = CYRILLIC_LANG["SR"]

GREEK = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i", "κ": "k",
    "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t",
    "υ": "y", "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o", "ά": "a", "έ": "e", "ή": "i", "ί": "i", "ό": "o",
    "ύ": "y", "ώ": "o", "ϊ": "i", "ϋ": "y", "ΐ": "i", "ΰ": "y",
}
GREEK_PAIRS = {"ου": "ou", "ού": "ou", "αι": "ai", "αί": "ai", "ει": "ei", "εί": "ei", "οι": "oi", "οί": "oi"}


def latinize(s, lang=""):
    """'София' -> 'Sofiya', 'Нови Сад' (Serbian) -> 'Novi Sad', 'Αθήνα' -> 'Athina'.
    Returns '' when the name has no Cyrillic or Greek letters."""
    cyr = any("\u0400" <= ch <= "\u04ff" for ch in s)
    greek = any("\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff" for ch in s)
    if not cyr and not greek:
        return ""
    table = {**CYRILLIC, **CYRILLIC_LANG.get(lang.upper(), {}), **GREEK}
    out = []
    i = 0
    while i < len(s):
        pair = s[i:i + 2].lower()
        if pair in GREEK_PAIRS:
            t = GREEK_PAIRS[pair]
            out.append(t.capitalize() if s[i] != s[i].lower() else t)
            i += 2
            continue
        ch = s[i]
        low = ch.lower()
        t = table.get(low)
        if t is None:
            out.append(ch)
        else:
            out.append(t.capitalize() if ch != low and t else t)
        i += 1
    return "".join(out)


# Well-known English forms that plain transliteration does not give.
LATIN_EXTRA = {"Sofiya": "Sofia", "Athina": "Athens", "Peiraias": "Piraeus"}


def latin_name(s, lang=""):
    """Display form of a Cyrillic or Greek name: 'София Централна' -> 'Sofia Tsentralna'; others unchanged."""
    t = latinize(s, lang)
    if not t:
        return s
    return re.sub(r"[^\W\d_]+", lambda m: LATIN_EXTRA.get(m.group(0), m.group(0)), t)


def fix_caps(s, cfg):
    """Names written in capitals ('caps_names' in feeds.json, the language's own rules for I/i)."""
    lang = cfg.get("caps_names")
    if not lang or not s or s != s.upper():
        return s
    s = re.sub(r"\s*\(", " (", s)

    def word(w):
        if len(w) <= 3 and not re.search(r"[AEIOUİÖÜ]", w):
            return w  # abbreviations: YHT, HT
        if lang == "tr":
            low = w.replace("I", "ı").replace("İ", "i").lower()
            head = {"i": "İ", "ı": "I"}.get(low[0], low[0].upper())
            return head + low[1:]
        return w.capitalize()

    return re.sub(r"[^\W\d_]+", lambda m: word(m.group(0)), s)


def transliterate(s):
    """German/Nordic spelling without special letters: München -> Muenchen."""
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"),
                 ("å", "aa"), ("ø", "oe"), ("æ", "ae"), ("ı", "i"), ("İ", "I")):
        s = s.replace(a, b)
    return s


def names_alike(a, b):
    ta, tb = set(norm_name(a)), set(norm_name(b))
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) > 0.5


def dist_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[max(a, b)] = min(a, b)


def merge_stations(feeds):
    """Gives every (space, station key) a global station id. Returns (node list, node->gid, stations)."""
    nodes = []          # (space, key, info, feed)
    node_of = {}
    for fd in feeds:
        for key, info in fd.stations.items():
            k = (fd.space, key)
            if k in node_of:
                continue
            node_of[k] = len(nodes)
            nodes.append((fd.space, key, info, fd))
    uf = UnionFind(len(nodes))

    # Same UIC number = same station, whatever the feed.
    by_uic = {}
    for i, (_, _, info, _) in enumerate(nodes):
        if info["uic"]:
            j = by_uic.setdefault(info["uic"], i)
            if j != i:
                uf.union(i, j)

    # Nearby stations from different id spaces.
    cell = {}
    for i, (_, _, info, _) in enumerate(nodes):
        if info["lat"] is None:
            continue
        cell.setdefault((int(info["lat"] * 100), int(info["lon"] * 100)), []).append(i)
    merged_pairs = 0
    for i, (space, _, info, _) in enumerate(nodes):
        if info["lat"] is None:
            continue
        cy, cx = int(info["lat"] * 100), int(info["lon"] * 100)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for j in cell.get((cy + dy, cx + dx), ()):
                    if j <= i:
                        continue
                    o = nodes[j][2]
                    d = dist_m(info["lat"], info["lon"], o["lat"], o["lon"])
                    if nodes[j][0] == space:
                        # Same source: only obvious duplicates (Hamburg Hbf listed twice).
                        ok = d <= MERGE_SAME_SOURCE_M and names_alike(info["name"], o["name"])
                    else:
                        ok = d <= MERGE_ANY_NAME_M or (d <= MERGE_SIMILAR_NAME_M and names_alike(info["name"], o["name"]))
                    if ok:
                        if uf.find(i) != uf.find(j):
                            merged_pairs += 1
                        uf.union(i, j)

    groups = {}
    for i in range(len(nodes)):
        groups.setdefault(uf.find(i), []).append(i)
    node_gid = [0] * len(nodes)
    stations = []
    for gid, members in enumerate(groups.values()):
        for m in members:
            node_gid[m] = gid
        stations.append({"members": members})
    log(f"stations: {len(nodes)} feed stations -> {len(stations)} merged ({merged_pairs} merges by distance)")
    return nodes, node_of, node_gid, stations


# ---------------------------------------------------------------- main build

def main():
    t0 = time.time()
    day0 = date.today()
    for d in (OUT, WORK):
        os.makedirs(d, exist_ok=True)
    cfgs = load_feeds()
    missing = [c["id"] for c in cfgs if not os.path.exists(os.path.join(RAW, c["id"] + ".zip"))]
    if missing:
        log("skipping feeds with no download: " + ", ".join(missing))
    cfgs = [c for c in cfgs if c["id"] not in missing]
    pack_countries = sorted({c["country"] for c in cfgs})
    feeds = [FeedData(c, day0) for c in cfgs]
    warnings = []

    nodes, node_of, node_gid, stations = merge_stations(feeds)

    # ---- global trips
    route_gid = {}
    routes = []
    trips = []   # dict: feed, src, route, stops[(gid, arr, dep, flags, platform)], mask, number, headsign, bikes
    dep_count = [0] * len(stations)
    feed_dep = [dict() for _ in stations]   # gid -> {feed country: departures}
    node_dep = [0] * len(nodes)             # departures per feed station, to pick the best name
    bad_trips = 0
    for fd in feeds:
        if fd.bad_stations:
            warnings.append(f"{fd.id}: {len(fd.bad_stations)} stations with broken coordinates skipped: "
                            + ", ".join(sorted(set(fd.bad_stations))[:20]))
        for tid, st in fd.trip_stops.items():
            t = fd.trips[tid]
            mask = fd.masks.get(t["service"], b"")
            if not any(mask):
                continue
            if any(fd.stations[fd.stop_station[x[0]][0]].get("bad") for x in st):
                bad_trips += 1
                continue
            rk = (fd.id, t["route"])
            if rk not in route_gid:
                route_gid[rk] = len(routes)
                r = fd.routes[t["route"]]
                routes.append(r)
            seq = []
            days = bin(int.from_bytes(mask, "little")).count("1")
            for si, arr, dep, flags in st:
                key, platform = fd.stop_station[si]
                node = node_of[(fd.space, key)]
                node_dep[node] += days
                gid = node_gid[node]
                if seq and seq[-1][0] == gid:      # same station twice in a row: keep one stop
                    seq[-1][2] = dep
                    seq[-1][3] &= flags
                    continue
                seq.append([gid, arr, dep, flags, platform])
            if len(seq) < 2:
                continue
            for s in seq[:-1]:
                if not s[3] & 1:
                    dep_count[s[0]] += days
                    feed_dep[s[0]][fd.country] = feed_dep[s[0]].get(fd.country, 0) + days
            trips.append({
                "feed": fd, "src": [f"{fd.id}:{tid.split('#')[0]}"] + ([f"{fd.id}~rt:{t['rt']}"] if t['rt'] else []), "route": route_gid[rk], "stops": seq,
                "mask": mask, "number": t["number"], "headsign": t["headsign"], "bikes": t["bikes"],
            })
    log(f"trips: {len(trips)} with service in the next {NDAYS} days ({bad_trips} dropped for broken stations)")

    # ---- station details: name, position, country, transfer time
    for gid, st in enumerate(stations):
        members = [nodes[m] for m in st["members"]]
        uic = next((info["uic"] for _, _, info, _ in members if info["uic"]), "")
        country = UIC_COUNTRY.get(uic[:2], "") if uic else ""
        if not country:
            fc = feed_dep[gid]
            country = max(fc, key=fc.get) if fc else members[0][3].country
            lat, lon = members[0][2]["lat"], members[0][2]["lon"]
            box = BOX.get(country)
            if box and lat is not None and not (box[0] <= lat <= box[2] and box[1] <= lon <= box[3]):
                country = "??"
        # Name: the busiest entry from a feed of the station's own country.
        home = [m for m in st["members"] if nodes[m][3].country == country and nodes[m][2]["name"]] \
            or [m for m in st["members"] if nodes[m][2]["name"]] or st["members"]
        best = nodes[max(home, key=lambda m: (nodes[m][3].cfg.get("name_priority", 1),
                                              not re.search(r"[,/]", tidy_name(nodes[m][2]["name"])), node_dep[m]))]
        names = sorted({m[2]["name"] for m in members if m[2]["name"]})
        names += [t for t in {transliterate(n) for n in names} if t not in names]
        latin = {latinize(n, best[3].country) for n in names} - {""}
        latin |= {LATIN_EXTRA[x] for x in latin if x in LATIN_EXTRA}
        names += [t for t in latin if t not in names]
        lats = [m[2]["lat"] for m in members if m[2]["lat"] is not None]
        lons = [m[2]["lon"] for m in members if m[2]["lon"] is not None]
        transfer = max([m[3].transfer.get(m[1], 0) for m in members] + [0]) or DEFAULT_TRANSFER
        lang = best[3].country
        shown = latin_name(tidy_name(best[2]["name"]), lang)
        if shown != tidy_name(best[2]["name"]):
            given = [n for n in names if not latinize(n) and names_alike(n, shown) and n not in latin]
            if given:
                shown = tidy_name(given[0])
        st.update({
            "name": shown, "alt": [n for n in names if n not in (shown, best[2]["name"]) or shown != tidy_name(best[2]["name"]) and n == best[2]["name"]],
            "lat": statistics.median(lats) if lats else 0.0, "lon": statistics.median(lons) if lons else 0.0,
            "country": country, "uic": uic, "transfer": transfer, "deps": dep_count[gid],
        })
    # Unknown country: take it from the nearest station that has a UIC number (within 30 km).
    known = [s for s in stations if s["uic"] and s["country"] not in ("", "??")]
    grid = {}
    for s in known:
        grid.setdefault((int(s["lat"] * 4), int(s["lon"] * 4)), []).append(s)
    for s in stations:
        if s["country"] != "??":
            continue
        cy, cx = int(s["lat"] * 4), int(s["lon"] * 4)
        near = [k for dy in (-1, 0, 1) for dx in (-1, 0, 1) for k in grid.get((cy + dy, cx + dx), ())]
        if near:
            k = min(near, key=lambda k: dist_m(s["lat"], s["lon"], k["lat"], k["lon"]))
            if dist_m(s["lat"], s["lon"], k["lat"], k["lon"]) <= 30000:
                s["country"] = k["country"]
                continue
        s["country"] = country_by_box(s["lat"], s["lon"])
    # English and other well-known names.
    with open(os.path.join(HERE, "aliases.json"), encoding="utf-8") as f:
        aliases = json.load(f)
    by_name = {}
    for s in stations:
        by_name.setdefault(s["name"], []).append(s)
    for station_name, extra in aliases.items():
        for s in by_name.get(station_name, []):
            s["alt"] = s["alt"] + [a for a in extra if a not in s["alt"]]
    missing = [n for n in aliases if n not in by_name]
    if missing:
        warnings.append("aliases for unknown station names: " + ", ".join(missing))
    # Busyness = departures per day the station's country has data for (a one-month feed is not "empty").
    country_days = {}
    for fd in feeds:
        union = 0
        for m in fd.masks.values():
            union |= int.from_bytes(m, "little")
        country_days[fd.country] = max(country_days.get(fd.country, 0), bin(union).count("1"))
    for gid, st in enumerate(stations):
        st["per_day"] = sum(n / max(1, country_days.get(c, NDAYS)) for c, n in feed_dep[gid].items())
    unknown = sum(1 for s in stations if s["country"] == "??")
    if unknown:
        warnings.append(f"{unknown} stations outside their feed's country and without a UIC number (country '??')")

    # ---- duplicates between feeds (same train in two feeds): same two consecutive stops at the same times
    trips = drop_duplicates(trips, stations, warnings)
    for i, t in enumerate(trips):
        t["gid"] = i

    # ---- packs
    version = day0.strftime("%Y%m%d")
    credits = [{"feed": c["id"], "name": c["name"], "licence": c["licence"], "credit": c["credit"],
                "url": c["source_page"]} for c in cfgs]
    credits.append({"feed": "osm", "name": "OpenStreetMap", "licence": "ODbL",
                    "credit": "© OpenStreetMap contributors (used by gtfs.de)", "url": "https://www.openstreetmap.org/copyright"})
    # feed stop id -> station, for feeds with live data ("live": true in feeds.json)
    source_stops = []
    for fd in feeds:
        if not fd.cfg.get("live"):
            continue
        for si, sid in enumerate(fd.stop_ids):
            key, _ = fd.stop_station[si]
            source_stops.append((fd.id, sid, node_gid[node_of[(fd.space, key)]]))
    log(f"live stop ids: {len(source_stops)}")
    files = []
    for cc in pack_countries:
        mine = [t for t in trips if any(stations[s[0]]["country"] == cc for s in t["stops"])
                or (t["feed"].country == cc and not any(stations[s[0]]["country"] in pack_countries for s in t["stops"]))]
        path = os.path.join(WORK, f"{cc.lower()}.sqlite")
        write_pack(path, cc, version, day0, mine, routes, stations, credits, source_stops)
        files.append(publish(path, cc, len(mine)))
        log(f"pack {cc}: {len(mine)} trips, {files[-1]['size'] / 1e6:.1f} MB compressed")

    idx = os.path.join(WORK, "stations_index.sqlite")
    write_index(idx, version, stations, trips, pack_countries)
    index_file = publish(idx, "index", len(stations))

    manifest = {
        "format": FORMAT_VERSION, "version": version, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "day0": day0.isoformat(), "days": NDAYS, "index": index_file, "packs": files, "credits": credits,
    }
    # Set by the GitHub Action: the Release these files are attached to.
    tag, repo = os.environ.get("RELEASE_TAG"), os.environ.get("GITHUB_REPOSITORY")
    if tag and repo:
        manifest["tag"] = tag
        manifest["base_url"] = f"https://github.com/{repo}/releases/download/{tag}/"
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write(f"built {manifest['built']} in {time.time() - t0:.0f}s\n")
        for fl in files + [index_file]:
            f.write(f"{fl['file']}: {fl['size'] / 1e6:.2f} MB, raw {fl['raw_size'] / 1e6:.1f} MB, {fl['count']} items\n")
        for w in warnings:
            f.write("WARNING " + w + "\n")
    for w in warnings:
        log("WARNING", w)
    log(f"done in {time.time() - t0:.0f}s")


def drop_duplicates(trips, stations, warnings):
    # Only stations that more than one feed knows can carry duplicates.
    multi = set()
    for gid, st in enumerate(stations):
        spaces = {m for m in st["members"]}
        if len(spaces) > 1:
            multi.add(gid)
    index = {}
    for i, t in enumerate(trips):
        s = t["stops"]
        for a, b in zip(s, s[1:]):
            if a[0] in multi and b[0] in multi:
                index.setdefault((a[0], a[2] % 86400, b[0], b[1] % 86400), []).append(i)
    uf = UnionFind(len(trips))
    for idxs in index.values():
        if len(idxs) < 2:
            continue
        for x in idxs[1:]:
            a, b = trips[idxs[0]], trips[x]
            if a["feed"].space != b["feed"].space and any(p & q for p, q in zip(a["mask"], b["mask"])):
                uf.union(idxs[0], x)
    groups = {}
    for i in range(len(trips)):
        groups.setdefault(uf.find(i), []).append(i)
    keep = []
    dropped = 0

    def pairs(t):
        s = t["stops"]
        return {(a[0], a[2] % 86400, b[0], b[1] % 86400) for a, b in zip(s, s[1:])}

    for members in groups.values():
        if len(members) == 1:
            keep.append(trips[members[0]])
            continue
        # Longest copies first. A copy is dropped only when the copies already kept run every one of its
        # hops at the same times; a copy that adds a section or a stop the others lack is kept, so a
        # border-to-border copy can never replace the full train. Feeds known to skip stops abroad
        # ("dup_priority": 0) go last and are dropped when half of them is already covered.
        order = sorted(members, key=lambda i: (trips[i]["feed"].cfg.get("dup_priority", 1), len(trips[i]["stops"])),
                       reverse=True)
        kept = []
        for i in order:
            t = trips[i]
            mine = pairs(t)
            covered = set()
            for k, kp in kept:
                covered |= mine & kp
            need = 0.5 if t["feed"].cfg.get("dup_priority", 1) == 0 else 1.0
            if kept and mine and len(covered) >= need * len(mine):
                owner = max(kept, key=lambda kk: len(mine & kk[1]))[0]
                owner["src"] += t["src"]
                if not owner["number"]:
                    owner["number"] = t["number"]
                dropped += 1
            else:
                kept.append((t, mine))
        keep.extend(t for t, _ in kept)
    log(f"duplicates: dropped {dropped} copies of trains that appear in two feeds")
    return keep


# ---------------------------------------------------------------- writing

def varints(values):
    out = bytearray()
    for v in values:
        v = max(0, int(v))
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v)
    return bytes(out)


PACK_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE stations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, lat REAL, lon REAL, country TEXT,
                      transfer INTEGER);
CREATE TABLE routes(id INTEGER PRIMARY KEY, kind INTEGER, type INTEGER, short TEXT, long TEXT, agency TEXT,
                    color INTEGER, pass INTEGER);
CREATE TABLE patterns(id INTEGER PRIMARY KEY, stops BLOB NOT NULL);
CREATE TABLE services(id INTEGER PRIMARY KEY, days BLOB NOT NULL);
CREATE TABLE trips(id INTEGER PRIMARY KEY, route INTEGER, pattern INTEGER, service INTEGER, start INTEGER,
                   times BLOB, number TEXT, headsign TEXT, platforms TEXT, flags BLOB, bikes INTEGER, src TEXT);
CREATE TABLE station_trips(station INTEGER, trip INTEGER, pos INTEGER, PRIMARY KEY(station, trip, pos))
  WITHOUT ROWID;
CREATE TABLE source_stops(feed TEXT, stop_id TEXT, station INTEGER, PRIMARY KEY(feed, stop_id)) WITHOUT ROWID;
"""


def write_pack(path, cc, version, day0, trips, routes, stations, credits, source_stops=()):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript("PRAGMA page_size=4096; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + PACK_SCHEMA)
    meta = {"format": FORMAT_VERSION, "country": cc, "version": version, "day0": day0.isoformat(),
            "days": NDAYS, "credits": json.dumps(credits, ensure_ascii=False)}
    db.executemany("INSERT INTO meta VALUES(?,?)", [(k, str(v)) for k, v in meta.items()])

    used_st, used_rt = set(), set()
    patterns, services = {}, {}
    rows, st_rows = [], []
    for t in trips:
        s = t["stops"]
        pkey = tuple(x[0] for x in s)
        pid = patterns.setdefault(pkey, len(patterns))
        sid = services.setdefault(t["mask"], len(services))
        used_st.update(pkey)
        used_rt.add(t["route"])
        vals = []
        prev = s[0][2]
        for k, x in enumerate(s):
            if k:
                vals.append(x[1] - prev)
            vals.append(x[2] - x[1])
            prev = x[2]
        platforms = "|".join(x[4] for x in s) if any(x[4] for x in s) else None
        flags = bytes(x[3] for x in s) if any(x[3] for x in s) else None
        headsign = t["headsign"] if t["headsign"] and t["headsign"] != stations[s[-1][0]]["name"] else None
        bikes = 1 if t["bikes"] == "1" else 2 if t["bikes"] == "2" else 0
        rows.append((t["gid"], t["route"], pid, sid, s[0][2], varints(vals), t["number"] or None, headsign,
                     platforms, flags, bikes, " ".join(t["src"])))
    db.executemany("INSERT INTO trips VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.executemany("INSERT INTO patterns VALUES(?,?)", [(i, varints(k)) for k, i in patterns.items()])
    db.executemany("INSERT INTO services VALUES(?,?)", [(i, m) for m, i in services.items()])
    db.executemany("INSERT INTO routes VALUES(?,?,?,?,?,?,?,?)", [
        (i, KIND_CODE[routes[i]["kind"]], routes[i]["type"], routes[i]["short"] or None, routes[i]["long"] or None,
         tidy_agency(routes[i]["agency"]) or None, routes[i]["color"], pass_code(routes[i]))
        for i in sorted(used_rt)])
    db.executemany("INSERT INTO stations VALUES(?,?,?,?,?,?)", [
        (g, stations[g]["name"], round(stations[g]["lat"], 6), round(stations[g]["lon"], 6), stations[g]["country"],
         stations[g]["transfer"]) for g in sorted(used_st)])
    for t in trips:
        for pos, x in enumerate(t["stops"]):
            st_rows.append((x[0], t["gid"], pos))
    db.executemany("INSERT OR IGNORE INTO station_trips VALUES(?,?,?)", st_rows)
    # Live feeds name stops by their own ids: map them to our stations (only stations in this pack).
    db.executemany("INSERT OR IGNORE INTO source_stops VALUES(?,?,?)",
                   [r for r in source_stops if r[2] in used_st])
    db.commit()
    db.execute("VACUUM")
    db.close()


def write_index(path, version, stations, trips, pack_countries):
    if os.path.exists(path):
        os.remove(path)
    packs_at = [set() for _ in stations]
    for t in trips:
        cs = {stations[s[0]]["country"] for s in t["stops"]} & set(pack_countries)
        if not cs:
            cs = {t["feed"].country}
        for s in t["stops"]:
            packs_at[s[0]] |= cs
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA page_size=4096; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
        CREATE TABLE stations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, alt TEXT, lat REAL, lon REAL,
                              country TEXT, uic TEXT, rank INTEGER, packs TEXT);
        CREATE VIRTUAL TABLE stations_fts USING fts5(name, alt, content='stations', content_rowid='id',
                              tokenize='unicode61 remove_diacritics 2', prefix='1 2 3');
    """)
    db.executemany("INSERT INTO meta VALUES(?,?)", [("format", str(FORMAT_VERSION)), ("version", version)])
    rows = []
    for g, s in enumerate(stations):
        if not packs_at[g]:
            continue
        rows.append((g, s["name"], " | ".join(s["alt"]) or None, round(s["lat"], 6), round(s["lon"], 6),
                     s["country"], s["uic"] or None, round(s.get("per_day", 0)), ",".join(sorted(packs_at[g]))))
    db.executemany("INSERT INTO stations VALUES(?,?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT INTO stations_fts(rowid, name, alt) SELECT id, name, coalesce(alt,'') FROM stations")
    db.execute("INSERT INTO stations_fts(stations_fts) VALUES('optimize')")
    db.commit()
    db.execute("VACUUM")
    db.close()


def publish(path, name, count):
    gz = os.path.join(OUT, os.path.basename(path) + ".gz")
    with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    h = hashlib.sha256()
    with open(gz, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"name": name, "file": os.path.basename(gz), "size": os.path.getsize(gz),
            "raw_size": os.path.getsize(path), "sha256": h.hexdigest(), "count": count}


if __name__ == "__main__":
    main()
