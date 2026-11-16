# Pack format (version 1)

Every file is SQLite, gzip-compressed for download (`*.sqlite.gz`). Built by `pipeline/build.py`.

## manifest.json
```json
{
  "format": 1, "version": "20261116", "built": "...", "day0": "2026-11-16", "days": 400,
  "index": {"name": "index", "file": "stations_index.sqlite.gz", "size": 0, "raw_size": 0, "sha256": "...", "count": 0},
  "packs": [{"name": "DE", "file": "de.sqlite.gz", "size": 0, "raw_size": 0, "sha256": "...", "count": 0}],
  "credits": [{"feed": "de-rv", "name": "...", "licence": "...", "credit": "...", "url": "..."}]
}
```
The app downloads `manifest.json`, compares `version` and `sha256`, downloads changed packs, checks the sha256, unzips and swaps.

**Where the app finds it:** always `https://github.com/abhayclaude910-cyber/railplanner-data/releases/latest/download/manifest.json`. That manifest has `base_url` pointing at its own Release (`.../releases/download/data-YYYYMMDD/`), so the packs it lists always come from the same build, even if a newer Release appears in between. Files = `base_url + file`.

## Ids
- **Station ids and trip ids are global across all packs in one build.** A trip that crosses a border is in every country pack it stops in, with the same id, so the app de-duplicates trips by `id` when it loads several packs.
- Ids change between builds, so the app must never store a pack id in user data. Saved journeys keep station names + times + `src`.

## stations_index.sqlite (shipped in the app)
| table | columns |
|---|---|
| `stations` | id, name, alt (other names, `" \| "`-separated), lat, lon, country (ISO, `??` = unknown), uic, rank (departures per day), packs (`DE,NL`: packs that serve it) |
| `stations_fts` | FTS5 over name + alt, diacritics removed, prefix search (`"amst"*`) |

## <cc>.sqlite (one per country)
| table | columns |
|---|---|
| `meta` | format, country, version, day0, days, credits (JSON) |
| `stations` | id, name, lat, lon, country, transfer (minimum transfer seconds) |
| `routes` | id, kind (0 rail, 1 ferry, 2 coach), type (GTFS route_type), short ("ICE", "IC", "S10"), long, agency, color, pass (0 normal, 1 rail passes usually not valid e.g. FlixTrain, 2 pass discount only e.g. mountain railways; from `pipeline/pass_rules.json`, show as a hint only) |
| `patterns` | id, stops: varint list of station ids |
| `services` | id, days: bitmask, bit *i* (byte i>>3, bit i&7) = runs on day0 + i |
| `trips` | id, route, pattern, service, start (first departure, seconds after service-day midnight, can be > 86400), times, number (train number), headsign (NULL = last stop's name), platforms (`"\|"`-joined per stop, NULL if none), flags (1 byte per stop: bit0 no boarding, bit1 no alighting; NULL if none), bikes (0 unknown, 1 yes, 2 no), src (space-separated live-data keys: `feed:trip_id` = the feed's static GTFS trip id, as used by GTFS-RT trip updates; `feed~rt:id` = the feed's own realtime id when it has one, e.g. Dutch `IFF:IC:2835`) |
| `station_trips` | station, trip, pos: which trips stop at a station, for departure boards |
| `source_stops` | feed, stop_id, station: the feed's own stop ids (platform level) mapped to our stations, only for feeds with live data. Live updates name stops by these ids, and also list stations a train passes without stopping |

**`times`** is a varint list. For stop 0: dwell (dep − arr, usually 0). For each next stop *n*: travel (arr*n* − dep*n−1*), then dwell (dep*n* − arr*n*). Absolute times come from `start` plus the running sum.

## Route search rules (reference: pipeline/query.py)
- A trip runs on service day D when its service bit for D is set. Times past midnight belong to day D.
- Connections are consecutive stop pairs. Skip boarding where flags bit0 is set, and alighting where bit1 is set.
- To change trains at a station, wait at least that station's `transfer` seconds.
