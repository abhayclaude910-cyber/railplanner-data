"""Streaming GTFS reader: reads only what the packs need, straight from the zip."""
import csv
import io
import zipfile
from datetime import date, timedelta

# Route types we keep (basic + extended GTFS types).
RAIL = set([2]) | set(range(100, 118))
FERRY = set([4]) | set(range(1000, 1100)) | set([1200])
COACH = set(range(200, 210))


def kind_of(route_type):
    """'rail' / 'ferry' / 'coach' for kept types, None for everything else (bus, tram, metro, cable...)."""
    if route_type in RAIL:
        return "rail"
    if route_type in FERRY:
        return "ferry"
    if route_type in COACH:
        return "coach"
    return None


class Feed:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.names = {}
        for n in self.zip.namelist():
            self.names[n.split("/")[-1]] = n

    def has(self, name):
        return name in self.names

    def table(self, name):
        """(column index dict, iterator of row lists): faster than rows() for huge files."""
        if name not in self.names:
            return {}, iter(())
        raw = self.zip.open(self.names[name])
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        header = [h.strip() for h in next(reader)]
        return {h: i for i, h in enumerate(header)}, reader

    def rows(self, name):
        """Yields dict rows of a GTFS file (empty if the file is missing)."""
        if name not in self.names:
            return
        with self.zip.open(self.names[name]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.reader(text)
            header = [h.strip() for h in next(reader)]
            for row in reader:
                if not row:
                    continue
                yield dict(zip(header, row))


def parse_time(s):
    """'25:03:00' -> seconds after service-day midnight, None when empty."""
    s = s.strip()
    if not s:
        return None
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def parse_date(s):
    s = s.strip()
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def service_days(feed, day0, ndays, wanted=None):
    """service_id -> bytearray bitmask of ndays starting at day0 (bit i = day0 + i runs).
    With `wanted`, only those service ids are built (calendar_dates can be huge)."""
    masks = {}
    nbytes = (ndays + 7) // 8

    def setbit(m, i, on):
        if 0 <= i < ndays:
            if on:
                m[i >> 3] |= 1 << (i & 7)
            else:
                m[i >> 3] &= ~(1 << (i & 7)) & 0xFF

    wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for r in feed.rows("calendar.txt"):
        if wanted is not None and r["service_id"] not in wanted:
            continue
        m = masks.setdefault(r["service_id"], bytearray(nbytes))
        start, end = parse_date(r["start_date"]), parse_date(r["end_date"])
        d = max(start, day0)
        last = min(end, day0 + timedelta(days=ndays - 1))
        while d <= last:
            if r[wd[d.weekday()]].strip() == "1":
                setbit(m, (d - day0).days, True)
            d += timedelta(days=1)
    for r in feed.rows("calendar_dates.txt"):
        if wanted is not None and r["service_id"] not in wanted:
            continue
        m = masks.setdefault(r["service_id"], bytearray(nbytes))
        i = (parse_date(r["date"]) - day0).days
        setbit(m, i, r["exception_type"].strip() == "1")
    return masks
