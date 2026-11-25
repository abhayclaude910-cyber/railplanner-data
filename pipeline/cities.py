"""City groups for the station index: "Milano (all stations)" = every station of one city.

A city is a set of stations in one country whose names start with the same city word(s) ("Milano Centrale",
"Milano Porta Garibaldi", "Milano Cadorna") and that lie within RADIUS_KM of the city's busiest station.
Names that start with a common prefix word ("Bad", "St.", "Den", "La", "San"...) use the first two words,
so "Bad Ems" and "Bad Kreuznach" never merge but "Den Haag Centraal" and "Den Haag HS" do.

Used by build.py (write_index); run on its own to print the groups of a built index:
    python pipeline/cities.py work/stations_index.sqlite [Paris London ...]
"""
import math
import re
import sys
import unicodedata

LINK_KM = 7.0  # a station joins the city when it is this close to one already in it...
MAX_KM = 30.0  # ...and this close to the city's busiest station
MIN_MEMBERS = 2
MIN_RANK = 400  # departures a day in the whole city: no entry for a village with two halts
MIN_MEMBER_RANK = 10

# Common English (and other language) names, so "Prague" or "Milan" finds the city too. Keyed by city_key.
CITY_NAMES = {
    "praha": ["Prague"], "wien": ["Vienna"], "milano": ["Milan"], "bruxelles": ["Brussels", "Brussel"],
    "brussel": ["Brussels", "Bruxelles"], "munchen": ["Munich"], "koln": ["Cologne"], "roma": ["Rome"],
    "warszawa": ["Warsaw"], "lisboa": ["Lisbon"], "kobenhavn": ["Copenhagen"], "den haag": ["The Hague"],
    "geneve": ["Geneva", "Genf"], "zurich": ["Zurich"], "torino": ["Turin"], "firenze": ["Florence"],
    "napoli": ["Naples"], "venezia": ["Venice"], "beograd": ["Belgrade"], "bucuresti": ["Bucharest"],
    "athina": ["Athens"], "krakow": ["Cracow"], "antwerpen": ["Antwerp", "Anvers"], "liege": ["Luik"],
    "gent": ["Ghent", "Gand"], "brugge": ["Bruges"], "nurnberg": ["Nuremberg"], "hannover": ["Hanover"],
    "basel": ["Basle", "Bale"], "luzern": ["Lucerne"], "bern": ["Berne"], "goteborg": ["Gothenburg"],
    "helsinki": ["Helsingfors"], "turku": ["Abo"], "sevilla": ["Seville"], "zaragoza": ["Saragossa"],
    "frankfurt": ["Francfort"], "strasbourg": ["Strassburg"], "marseille": ["Marseilles"],
    "lyon": ["Lyons"], "thessaloniki": ["Salonica"], "gdansk": ["Danzig"], "wroclaw": ["Breslau"],
    "poznan": ["Posen"], "bratislava": ["Pressburg"], "ljubljana": ["Laibach"], "salzburg": [],
}

# First words that do not name a city on their own: take the next word too.
PREFIXES = {
    "bad", "st", "st.", "sankt", "saint", "sainte", "san", "santa", "santo", "sao", "sant", "den", "de", "het",
    "la", "le", "les", "l", "el", "il", "lo", "los", "las", "the", "s", "'s", "nieuw", "new", "old", "alt",
    "neu", "nowy", "nova", "novi", "novo", "nove", "stary", "stara", "velke", "velika", "veliko", "mala",
    "gross", "grosse", "klein", "hohen", "bourg", "port", "porto", "mont", "monte", "castel", "villa",
    "villeneuve", "lahden", "west", "east", "north", "south", "upper", "lower", "great", "little", "kings",
    "ober", "unter", "nieder", "markt", "stadt", "flughafen", "aeroport", "airport", "gare", "station",
    "bahnhof", "estacion", "stazione", "dworzec", "halte", "mount", "fort", "black", "white", "red",
}

# Names whose first word is a city word but which are not in that city's group (airports, other towns).
NOT_A_CITY_WORD = {"airport", "flughafen", "aeroport", "aeroporto", "aeropuerto", "lotnisko", "lentoasema",
                   "hauptbahnhof", "hbf", "bahnhof", "bf", "centraal", "centrale", "central", "centrum",
                   "gare", "station", "stazione", "estacion", "nord", "sud", "est", "ouest", "midi"}

# S-Bahn / U-Bahn marks in front of German and Austrian names.
TRANSIT_MARKS = {"s", "u", "sbahn", "ubahn"}
_TRAILING_CITY = re.compile(r"\(([^()]+)\)\s*$")

_SPLIT = re.compile(r"[^\w']+", re.UNICODE)


def plain(word):
    """Lower case, accents removed ("Zürich" -> "zurich")."""
    w = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in w if not unicodedata.combining(c))


def city_words(name):
    """The words that name the city in a station name, in their original spelling, or None.
    "Frankfurt(Main) Hbf" -> ["Frankfurt"], "Den Haag HS" -> ["Den", "Haag"], "Paris-Nord" -> ["Paris"]."""
    words = [w for w in _SPLIT.split(name) if w and not w.isdigit()]
    while words and plain(words[0]) in TRANSIT_MARKS:  # "S+U Berlin Hbf", "S Westkreuz (Berlin)"
        words = words[1:]
    if not words:
        return None
    n = 2 if plain(words[0]).strip("'") in PREFIXES or len(words[0]) <= 2 else 1
    if len(words) < n:
        return None
    out = words[:n]
    if any(plain(w) in NOT_A_CITY_WORD for w in out):
        return None
    if len(plain("".join(out))) < 3:
        return None
    return out


def city_key(name):
    w = city_words(name)
    return None if w is None else " ".join(plain(x) for x in w)


def km(a, b):
    dlat = math.radians(b["lat"] - a["lat"])
    dlon = math.radians(b["lon"] - a["lon"]) * math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return 6371 * math.hypot(dlat, dlon)


def groups(stations):
    """stations: list of dicts with id, name, alt (list), country, lat, lon, rank.
    Returns city dicts: name ("Milano"), alt (list), country, lat, lon, rank, members (ids, busiest first)."""
    keys = [city_key(s["name"]) for s in stations]
    known = {(s["country"], k) for s, k in zip(stations, keys) if k}
    by_key = {}
    for s, k in zip(stations, keys):
        # "S Westkreuz (Berlin)": an S/U-Bahn stop with the city in brackets at the end, when that is a city
        # word in this country. Only for S/U stops: "Bühl (Baden)" is not in Baden-Baden.
        m = _TRAILING_CITY.search(s["name"])
        if m and plain(_SPLIT.split(s["name"])[0]) in TRANSIT_MARKS:
            pk = city_key(m.group(1))
            if pk and pk != k and (s["country"], pk) in known:
                k = pk
        if k:
            by_key.setdefault((s["country"], k), []).append(s)
    out = []
    for (country, key), members in by_key.items():
        if len(members) < MIN_MEMBERS:
            continue
        members.sort(key=lambda s: -s["rank"])
        # Several towns can share a first word ("Frankfurt (Main)" and "Frankfurt (Oder)"): start at the
        # busiest station, grow the city station by station (each within LINK_KM of one already in it), and
        # repeat with what is left.
        left = members
        while len(left) >= MIN_MEMBERS:
            centre = left[0]
            near = [centre]
            rest = left[1:]
            grew = True
            while grew:
                grew = False
                for s in list(rest):
                    if km(centre, s) <= MAX_KM and any(km(m, s) <= LINK_KM for m in near):
                        near.append(s)
                        rest.remove(s)
                        grew = True
            left = rest
            if len(near) < MIN_MEMBERS:
                continue
            near.sort(key=lambda s: -s["rank"])
            if sum(s["rank"] for s in near) < MIN_RANK or sum(s["rank"] >= MIN_MEMBER_RANK for s in near) < 2:
                continue
            out.append(_city(country, key, centre, near))
    # Two towns with the same name in one country: add the busiest station's "(Main)" / "(Oder)" part.
    same = {}
    for c in out:
        same.setdefault((c["country"], c["name"]), []).append(c)
    for cs in same.values():
        if len(cs) > 1:
            for c in cs:
                q = re.search(r"\(([^)]+)\)", c["centre"])
                c["name"] = f"{c['name']} ({q.group(1) if q else c['centre']})"
    return out


def _city(country, key, centre, near):
    words = city_words(centre["name"])
    in_names = {plain(w) for s in near for w in _SPLIT.split(s["name"]) if w}
    alts = list(CITY_NAMES.get(key, []))
    # Other-language names the stations carry ("Brussel-Zuid" -> "Brussel"), but not words of the station
    # names themselves ("London St Pancras" has the alt "St Pancras International": not a city name).
    for s in near[:5]:
        for a in s.get("alt") or []:
            w = city_words(a)
            if not w:
                continue
            k = " ".join(plain(x) for x in w)
            alt = " ".join(w)
            if k != key and not any(plain(x) in in_names for x in w) and plain(alt) not in map(plain, alts):
                alts.append(alt)
    return {
        "name": " ".join(words), "alt": alts, "country": country, "key": key, "centre": centre["name"],
        "lat": sum(s["lat"] for s in near) / len(near), "lon": sum(s["lon"] for s in near) / len(near),
        "rank": sum(s["rank"] for s in near), "members": [s["id"] for s in near],
    }


CITY_ID_BASE = 10_000_000  # city rows in the stations table: id >= this, never a real station id


def add_to_index(db):
    """Adds the city rows to a station index database (stations table with a members column).
    The searchable name is the plain city name; the shown name is "Milano (all stations)"."""
    rows = [{"id": r[0], "name": r[1], "alt": r[2].split(" | ") if r[2] else [], "country": r[3],
             "lat": r[4], "lon": r[5], "rank": r[6] or 0, "packs": (r[7] or "").split(",")}
            for r in db.execute("SELECT id, name, alt, country, lat, lon, rank, packs FROM stations "
                                "WHERE members IS NULL")]
    packs = {r["id"]: r["packs"] for r in rows}
    cities = sorted(groups(rows), key=lambda c: -c["rank"])
    db.executemany("INSERT INTO stations(id, name, alt, lat, lon, country, uic, rank, packs, members) "
                   "VALUES(?,?,?,?,?,?,NULL,?,?,?)", [
                       (CITY_ID_BASE + i, label(c), " | ".join([c["name"]] + c["alt"]), round(c["lat"], 6),
                        round(c["lon"], 6), c["country"], c["rank"],
                        ",".join(sorted({p for m in c["members"] for p in packs[m] if p})),
                        ",".join(map(str, c["members"])))
                       for i, c in enumerate(cities)])
    return len(cities)


def label(city):
    return f"{city['name']} (all stations)"


if __name__ == "__main__":
    import sqlite3
    db = sqlite3.connect(sys.argv[1])
    rows = [{"id": r[0], "name": r[1], "alt": (r[2] or "").split(" | ") if r[2] else [], "country": r[3],
             "lat": r[4], "lon": r[5], "rank": r[6]}
            for r in db.execute("SELECT id, name, alt, country, lat, lon, rank FROM stations")]
    names = {r["id"]: r["name"] for r in rows}
    gs = groups(rows)
    print(len(gs), "cities")
    want = [plain(w) for w in sys.argv[2:]]
    for c in sorted(gs, key=lambda c: -c["rank"]):
        if want and c["key"] not in want:
            continue
        print(f"{label(c)} [{c['country']}] alt={c['alt']} rank={c['rank']} n={len(c['members'])}: "
              + ", ".join(names[m] for m in c["members"][:12]) + (" ..." if len(c["members"]) > 12 else ""))
