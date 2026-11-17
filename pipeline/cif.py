"""Great Britain: National Rail timetable (RSPS5046 "CIF", the DTD timetable zip) -> GTFS.

The zip holds fixed-width text files:
  *.MSN  master station names: TIPLOC -> CRS code, name, Ordnance Survey grid position
  *.MCA  the schedules: BS (basic schedule) + BX (extra) + LO / LI / LT (origin, calling points, end)

Schedules have an STP indicator: P = permanent, O = overlay, N = new short-term, C = cancellation.
On a given day the short-term ones (C, O, N) replace the permanent schedule of the same train UID:
the permanent schedule becomes a GTFS calendar with removal exceptions on those days.

    python pipeline/cif.py raw/gb.src.zip raw/gb.zip
"""
import csv
import io
import math
import re
import sys
import zipfile
from datetime import date, timedelta

# ATOC operator codes -> (name, short label shown before the train)
OPERATORS = {
    "AW": ("Transport for Wales", "TfW"), "CC": ("c2c", "c2c"), "CH": ("Chiltern Railways", "Chiltern"),
    "CS": ("Caledonian Sleeper", "Caledonian Sleeper"), "EM": ("East Midlands Railway", "EMR"),
    "ES": ("Eurostar", "Eurostar"), "GC": ("Grand Central", "Grand Central"), "GN": ("Great Northern", "Great Northern"),
    "GR": ("LNER", "LNER"), "GW": ("Great Western Railway", "GWR"), "GX": ("Gatwick Express", "Gatwick Express"),
    "HT": ("Hull Trains", "Hull Trains"), "HX": ("Heathrow Express", "Heathrow Express"), "IL": ("Island Line", "Island Line"),
    "LD": ("Lumo", "Lumo"), "LE": ("Greater Anglia", "Greater Anglia"), "LM": ("West Midlands Trains", "WMT"),
    "LO": ("London Overground", "Overground"), "ME": ("Merseyrail", "Merseyrail"), "NT": ("Northern", "Northern"),
    "SE": ("Southeastern", "Southeastern"), "SN": ("Southern", "Southern"), "SR": ("ScotRail", "ScotRail"),
    "SW": ("South Western Railway", "SWR"), "SX": ("Stansted Express", "Stansted Express"),
    "TL": ("Thameslink", "Thameslink"), "TP": ("TransPennine Express", "TPE"), "VT": ("Avanti West Coast", "Avanti"),
    "XC": ("CrossCountry", "CrossCountry"), "XR": ("Elizabeth line", "Elizabeth line"), "TW": ("Tyne and Wear Metro", "Metro"),
    "WR": ("West Coast Railways", "West Coast Railways"), "NY": ("North Yorkshire Moors Railway", "NYMR"),
    "LT": ("London Underground", "Underground"), "SJ": ("Sheffield Supertram", "Supertram"),
}
# Train categories kept (passenger trains); buses (BR, BS), metro/underground (OL) and freight are dropped.
RAIL_CATEGORIES = {"OO", "OW", "XC", "XD", "XI", "XR", "XU", "XX", "XZ", "  ", ""}
SHIP_CATEGORIES = {"SS"}


def os_grid_to_wgs84(e, n):
    """Ordnance Survey National Grid (OSGB36) easting/northing in metres -> WGS84 (lat, lon).
    Transverse Mercator inverse on the Airy 1830 ellipsoid, then a Helmert shift (a few metres off)."""
    a, b = 6377563.396, 6356256.909
    f0, lat0, lon0 = 0.9996012717, math.radians(49), math.radians(-2)
    n0, e0 = -100000, 400000
    e2 = 1 - (b * b) / (a * a)
    nn = (a - b) / (a + b)
    lat, m = lat0, 0.0
    while True:
        lat = (n - n0 - m) / (a * f0) + lat
        ma = (1 + nn + 1.25 * nn ** 2 + 1.25 * nn ** 3) * (lat - lat0)
        mb = (3 * nn + 3 * nn ** 2 + 2.625 * nn ** 3) * math.sin(lat - lat0) * math.cos(lat + lat0)
        mc = (1.875 * nn ** 2 + 1.875 * nn ** 3) * math.sin(2 * (lat - lat0)) * math.cos(2 * (lat + lat0))
        md = 35 / 24 * nn ** 3 * math.sin(3 * (lat - lat0)) * math.cos(3 * (lat + lat0))
        m = b * f0 * (ma - mb + mc - md)
        if abs(n - n0 - m) < 0.00001:
            break
    s = math.sin(lat)
    nu = a * f0 / math.sqrt(1 - e2 * s * s)
    rho = a * f0 * (1 - e2) / (1 - e2 * s * s) ** 1.5
    eta2 = nu / rho - 1
    t = math.tan(lat)
    sec = 1 / math.cos(lat)
    vii = t / (2 * rho * nu)
    viii = t / (24 * rho * nu ** 3) * (5 + 3 * t * t + eta2 - 9 * t * t * eta2)
    ix = t / (720 * rho * nu ** 5) * (61 + 90 * t * t + 45 * t ** 4)
    x = sec / nu
    xi = sec / (6 * nu ** 3) * (nu / rho + 2 * t * t)
    xii = sec / (120 * nu ** 5) * (5 + 28 * t * t + 24 * t ** 4)
    xiia = sec / (5040 * nu ** 7) * (61 + 662 * t * t + 1320 * t ** 4 + 720 * t ** 6)
    de = e - e0
    phi = lat - vii * de ** 2 + viii * de ** 4 - ix * de ** 6
    lam = lon0 + x * de - xi * de ** 3 + xii * de ** 5 - xiia * de ** 7
    # OSGB36 -> WGS84 (Helmert, via cartesian)
    h = 0
    nu2 = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    x1 = (nu2 + h) * math.cos(phi) * math.cos(lam)
    y1 = (nu2 + h) * math.cos(phi) * math.sin(lam)
    z1 = ((1 - e2) * nu2 + h) * math.sin(phi)
    tx, ty, tz, sc = 446.448, -125.157, 542.060, -20.4894e-6
    rx, ry, rz = (math.radians(v / 3600) for v in (0.1502, 0.2470, 0.8421))
    x2 = tx + (1 + sc) * x1 - rz * y1 + ry * z1
    y2 = ty + rz * x1 + (1 + sc) * y1 - rx * z1
    z2 = tz - ry * x1 + rx * y1 + (1 + sc) * z1
    a2, b2 = 6378137.0, 6356752.3142
    e22 = 1 - (b2 * b2) / (a2 * a2)
    p = math.hypot(x2, y2)
    lat2 = math.atan2(z2, p * (1 - e22))
    for _ in range(10):
        nu3 = a2 / math.sqrt(1 - e22 * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + e22 * nu3 * math.sin(lat2), p)
    return math.degrees(lat2), math.degrees(math.atan2(y2, x2))


main_names = {}


def read_stations(text):
    """MSN 'A' records -> {tiploc: (crs, name, lat, lon)}. Also fills main_names: CRS -> the station's main
    record (the one whose own code is the station code)."""
    out = {}
    main_names.clear()
    for line in text.splitlines():
        if not line.startswith("A") or len(line) < 63 or line.startswith("A    FILE-SPEC"):
            continue
        name = line[5:35].strip()
        tiploc = line[36:43].strip()
        crs = line[49:52].strip() or line[43:46].strip()
        try:
            east = (int(line[52:57]) - 10000) * 100
            north = (int(line[58:63]) - 60000) * 100
        except ValueError:
            continue
        if not tiploc or not crs or east <= 0 or north <= 0:
            continue
        lat, lon = os_grid_to_wgs84(east, north)
        out[tiploc] = (crs, tidy_station(name), lat, lon)
        if line[43:46].strip() == crs and crs not in main_names:
            main_names[crs] = (tidy_station(name), lat, lon)
    return out


def tidy_station(name):
    """'LONDON KINGS CROSS' -> 'London Kings Cross'; keeps short codes in capitals."""
    def word(w):
        if len(w) <= 2 and w not in ("OF", "ON", "LE", "LA", "DE", "BY", "UP"):
            return w
        return w.capitalize()
    s = re.sub(r"[A-Za-z]+", lambda m: word(m.group(0)), name.title() if name.isupper() else name)
    s = re.sub(r"\b(Of|On|Le|La|De|By|Upon|And|The|In|Under)\b", lambda m: m.group(0).lower(), s)
    s = re.sub(r"\(([^)]*)\)", lambda m: "(" + m.group(1)[:1].upper() + m.group(1)[1:] + ")", s)
    return s[:1].upper() + s[1:]


def parse_date(s):
    return date(2000 + int(s[0:2]), int(s[2:4]), int(s[4:6]))


def hhmm(s):
    """'1234' / '1234H' -> seconds; '' or '0000' handled by the caller."""
    s = s.strip()
    if len(s) < 4 or not s[:4].isdigit():
        return None
    return int(s[0:2]) * 3600 + int(s[2:4]) * 60 + (30 if s[4:5] == "H" else 0)


def days_of(start, end, mask, stop_after=None):
    """Dates from start to end whose weekday is set in mask ('1111100' = Mon-Fri)."""
    d = start
    out = []
    while d <= end:
        if mask[d.weekday()] == "1":
            out.append(d)
        d += timedelta(days=1)
        if stop_after and len(out) > stop_after:
            break
    return out


def read_schedules(lines):
    """MCA lines -> schedules, one at a time (the file is ~700 MB, so it is never held in memory):
    dict(uid, start, end, days, stp, status, category, atoc, stops=[(tiploc, arr, dep, platform)])."""
    cur = None
    for line in lines:
        rt = line[:2]
        if rt == "BS":
            cur = None
            if line[2:3] == "D":  # delete record: not used in a full extract
                continue
            stp = line[79:80] or "P"
            cur = {"uid": line[3:9], "start": line[9:15], "end": line[15:21].strip() or line[9:15], "days": line[21:28],
                   "status": line[29:30], "category": line[30:32], "stp": stp, "atoc": "", "stops": []}
            if stp == "C":  # cancellations have no location records
                yield cur
                cur = None
        elif cur is None:
            continue
        elif rt == "BX":
            cur["atoc"] = line[11:13].strip()
        elif rt == "LO":
            pub = line[15:19]
            d = hhmm(pub) if pub.strip() and pub != "0000" else hhmm(line[10:15])
            if d is not None:
                cur["stops"].append((line[2:9].strip(), d, d, line[19:22].strip()))
        elif rt == "LI":
            if line[20:25].strip():  # passing point
                continue
            pa, pd = line[25:29], line[29:33]
            if pa == "0000" and pd == "0000":  # not advertised (crew change, operational stop)
                continue
            arr, dep = hhmm(pa), hhmm(pd)
            if arr is None and dep is None:
                continue
            cur["stops"].append((line[2:9].strip(), arr if arr is not None else dep, dep if dep is not None else arr,
                                 line[33:36].strip()))
        elif rt == "LT":
            pub = line[15:19]
            a = hhmm(pub) if pub.strip() and pub != "0000" else hhmm(line[10:15])
            if a is not None:
                cur["stops"].append((line[2:9].strip(), a, a, line[19:22].strip()))
            yield cur
            cur = None


def convert(src_zip, out_zip, today=None, window_days=400):
    import os
    import tempfile

    today = today or date.today()
    z = zipfile.ZipFile(src_zip)
    names = z.namelist()

    def member(ext):
        f = next((n for n in names if n.upper().endswith(ext)), None)
        if f is None:
            raise FileNotFoundError(f"{ext} not in {names}")
        return f

    def lines(ext):
        return io.TextIOWrapper(z.open(member(ext)), encoding="latin-1", newline=None)

    stations = read_stations(z.read(member(".MSN")).decode("latin-1"))
    first = today - timedelta(days=1)
    last = today + timedelta(days=window_days)

    # Pass 1: the days on which short-term schedules (C, O, N) replace a train's permanent schedule.
    taken = {}
    n_sched = 0
    with lines(".MCA") as f:
        for line in f:
            if line.startswith("BS") and line[2:3] != "D":
                n_sched += 1
                stp = line[79:80]
                if stp in ("C", "O", "N"):
                    start, end = parse_date(line[9:15]), parse_date(line[15:21].strip() or line[9:15])
                    for d in days_of(max(start, first), min(end, last), line[21:28]):
                        taken.setdefault(line[3:9], set()).add(d)

    # Pass 2: trips; stop_times go straight to a temporary file.
    routes, trips, calendar, cal_dates, used = {}, [], [], [], set()
    n_trip = 0
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", suffix=".txt", delete=False)
    st_writer = csv.writer(tmp, lineterminator="\n")
    st_writer.writerow(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "platform_code"])
    try:
        with lines(".MCA") as f:
            for s in read_schedules(f):
                if s["stp"] == "C" or s["status"] in ("B", "F", "T", "2", "3", "4", "5") or len(s["stops"]) < 2:
                    continue
                cat = s["category"]
                if cat in SHIP_CATEGORIES:
                    rtype = 4
                elif cat in RAIL_CATEGORIES or (cat[:1] in ("X", "O") and cat != "OL"):
                    rtype = 105 if cat == "XZ" else (101 if s["atoc"] == "ES" else 2)
                else:
                    continue
                start, end = parse_date(s["start"]), parse_date(s["end"])
                if end < first or start > last:
                    continue
                stops = [st for st in s["stops"] if st[0] in stations]
                if len(stops) < 2:
                    continue
                n_trip += 1
                sid = f"s{n_trip}"
                calendar.append((sid, *s["days"], start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
                if s["stp"] == "P":
                    for d in sorted(taken.get(s["uid"], ())):
                        if start <= d <= end and s["days"][d.weekday()] == "1":
                            cal_dates.append((sid, d.strftime("%Y%m%d"), 2))
                atoc = s["atoc"] or "ZZ"
                name, short = OPERATORS.get(atoc, (atoc, atoc))
                rid = f"{atoc}-{rtype}"
                routes[rid] = (rid, atoc, short, name if rtype != 105 else f"{name} sleeper", rtype)
                tid = f"{s['uid']}-{s['stp']}-{n_trip}"
                trips.append((rid, sid, tid, stations[stops[-1][0]][1]))
                prev, add = -1, 0
                seq = 0
                last_crs = None
                for tip, arr, dep, plat in stops:
                    if arr + add < prev:  # past midnight
                        add += 86400
                    a, d = arr + add, dep + add
                    if d < a:
                        d += 86400
                        add += 86400
                    prev = d
                    crs = stations[tip][0]
                    if crs == last_crs:  # two TIPLOCs of one station in a row
                        continue
                    last_crs = crs
                    used.add(tip)
                    seq += 1
                    st_writer.writerow((tid, fmt(a), fmt(d), crs, seq, plat))
        tmp.close()

        crs_info = {}
        for tip in used:
            crs, name, lat, lon = stations[tip]
            crs_info.setdefault(crs, (name, lat, lon))
        for crs in crs_info:  # the station's main record names it ("Glasgow Central", not "Glasgow Cent Ll")
            if crs in main_names:
                n, la, lo = main_names[crs]
                crs_info[crs] = (n, la, lo)

        def table(zo, name, header, rows):
            out = io.StringIO()
            w = csv.writer(out, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
            zo.writestr(name, out.getvalue())

        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zo:
            agencies = sorted({r[1] for r in routes.values()})
            table(zo, "agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone"],
                  [(a, OPERATORS.get(a, (a,))[0], "https://www.nationalrail.co.uk/", "Europe/London") for a in agencies])
            table(zo, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon"],
                  [(c, n, f"{la:.6f}", f"{lo:.6f}") for c, (n, la, lo) in sorted(crs_info.items())])
            table(zo, "routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
                  routes.values())
            table(zo, "trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign"], trips)
            table(zo, "calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                       "sunday", "start_date", "end_date"], calendar)
            table(zo, "calendar_dates.txt", ["service_id", "date", "exception_type"], cal_dates)
            zo.write(tmp.name, "stop_times.txt")
    finally:
        tmp.close()
        os.unlink(tmp.name)
    return {"schedules": n_sched, "trips": len(trips), "stations": len(crs_info), "routes": len(routes),
            "removed_days": len(cal_dates)}


def fmt(t):
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


if __name__ == "__main__":
    print(convert(sys.argv[1], sys.argv[2]))
