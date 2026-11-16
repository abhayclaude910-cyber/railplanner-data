"""Test tool: station search and earliest-arrival journeys straight from the built packs.

    python pipeline/query.py find "Amsterdam"
    python pipeline/query.py route "Muenchen Hbf" "Amsterdam Centraal" 2026-11-20 08:00 [de nl ch]

It reads the uncompressed packs in ../work, so run build.py first. This is the reference for the
app's Dart route search (Connection Scan Algorithm): same data, same rules.
"""
import os
import sqlite3
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(os.path.dirname(HERE), "work")
sys.stdout.reconfigure(encoding="utf-8")


def unvarints(b):
    out, v, shift = [], 0, 0
    for byte in b:
        v |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            out.append(v)
            v, shift = 0, 0
    return out


def find(text, limit=8):
    db = sqlite3.connect(os.path.join(WORK, "stations_index.sqlite"))
    q = " ".join(f'"{w}"*' for w in text.replace('"', "").split())
    rows = db.execute(
        "SELECT s.id, s.name, s.country, s.rank, s.packs, s.alt FROM stations_fts f JOIN stations s ON s.id=f.rowid "
        "WHERE stations_fts MATCH ? ORDER BY s.rank DESC LIMIT ?", (q, limit)).fetchall()
    return rows


def hhmm(sec):
    d, s = divmod(sec, 86400)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}" + (f" (+{d}d)" if d else "")


def route(origin, dest, day, start, packs):
    # Load trips running on `day` (and the day before, for trips past midnight).
    stations, routes, trips = {}, {}, {}
    conns = []
    seen = set()
    for cc in packs:
        db = sqlite3.connect(os.path.join(WORK, f"{cc}.sqlite"))
        day0 = date.fromisoformat(db.execute("SELECT value FROM meta WHERE key='day0'").fetchone()[0])
        for sid, name, transfer in db.execute("SELECT id, name, transfer FROM stations"):
            stations[sid] = (name, transfer)
        for rid, short, kind in db.execute("SELECT id, short, kind FROM routes"):
            routes[rid] = (short or "", kind)
        pats = {pid: unvarints(b) for pid, b in db.execute("SELECT id, stops FROM patterns")}
        svc = {sid: b for sid, b in db.execute("SELECT id, days FROM services")}
        for offset in (0, -1):
            i = (day - day0).days + offset
            if i < 0:
                continue
            for tid, rid, pid, sv, st, times, number, head, plats, flags in db.execute(
                    "SELECT id, route, pattern, service, start, times, number, headsign, platforms, flags FROM trips"):
                m = svc[sv]
                if (tid, offset) in seen or i >> 3 >= len(m) or not m[i >> 3] >> (i & 7) & 1:
                    continue
                seen.add((tid, offset))
                base = offset * 86400
                stops = pats[pid]
                v = unvarints(times)
                arr, dep = [st], []
                t, k = st, 0
                for n in range(len(stops)):
                    if n:
                        t += v[k]; k += 1
                        arr.append(t)
                    t += v[k]; k += 1
                    dep.append(t)
                fl = flags or bytes(len(stops))
                pl = plats.split("|") if plats else [""] * len(stops)
                trips[(tid, offset)] = (rid, number, head, stops, pl)
                for n in range(len(stops) - 1):
                    if fl[n] & 1 or fl[n + 1] & 2:
                        continue
                    conns.append((dep[n] + base, arr[n + 1] + base, stops[n], stops[n + 1], (tid, offset), n))
    conns.sort()

    # Connection Scan: earliest arrival with per-station minimum transfer time.
    INF = 1 << 40
    best = {origin: start}
    trip_in = {}      # trip -> connection index where we boarded
    via = {}          # station -> (board conn, alight conn)
    for ci, (d, a, s1, s2, trip, n) in enumerate(conns):
        if d < start:
            continue
        if best.get(dest, INF) <= d:
            break
        if trip in trip_in or (best.get(s1, INF) + (0 if s1 == origin else stations[s1][1])) <= d:
            if trip not in trip_in:
                trip_in[trip] = ci
            if a < best.get(s2, INF):
                best[s2] = a
                via[s2] = (trip_in[trip], ci)
    if dest not in via:
        return None
    legs = []
    s = dest
    while s != origin:
        b, e = via[s]
        legs.append((conns[b], conns[e]))
        s = conns[b][2]
    legs.reverse()
    out = []
    for b, e in legs:
        rid, number, head, stops, pl = trips[b[4]]
        short, kind = routes[rid]
        out.append(f"{hhmm(b[0])} {stations[b[2]][0]} {('pl. ' + pl[b[5]]) if pl[b[5]] else ''}".rstrip()
                   + f"  ->  {hhmm(e[1])} {stations[e[3]][0]}   [{short} {number or ''}".rstrip() + f"] to {head or stations[stops[-1]][0]}")
    return out


def main():
    if sys.argv[1] == "find":
        for r in find(sys.argv[2]):
            print(r)
        return
    _, _, a, b, d, t, *packs = sys.argv
    packs = packs or ["de", "nl", "ch"]
    sa, sb = find(a, 1), find(b, 1)
    if not sa or not sb:
        print("station not found", sa, sb)
        return
    print(f"{sa[0][1]} ({sa[0][2]}) -> {sb[0][1]} ({sb[0][2]})  {d} {t}  packs {packs}")
    hh, mm = map(int, t.split(":"))
    t0 = datetime.now()
    res = route(sa[0][0], sb[0][0], date.fromisoformat(d), hh * 3600 + mm * 60, packs)
    print("\n".join(res) if res else "no journey found", f"\n({(datetime.now() - t0).total_seconds():.1f}s)")


if __name__ == "__main__":
    main()
