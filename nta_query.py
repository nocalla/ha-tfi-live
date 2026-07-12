#!/usr/bin/env python3
"""Standalone CLI to query the NTA GTFS-RT feed for upcoming departures at a stop.

Usage:
    python nta_query.py --api-key KEY --stop-id 8220DB000836 --route-id 46A
    python nta_query.py --api-key KEY --stop-id 8220DB000836 --route-id 46A \
        --direction 0
    python nta_query.py --api-key KEY --stop-id 8220DB000836 --route-id 46A --json

No Home Assistant or third-party libraries required — stdlib only.
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

_FEED_URL = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates?format=json"
_DUBLIN_TZ = ZoneInfo("Europe/Dublin")


# --- Parsing logic mirrored from coordinator.py:TfiLiveCoordinator._parse_feed ---


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_feed(payload: dict) -> list[dict]:
    entities = []
    for entity in payload.get("entity", []):
        trip_update = entity.get("trip_update")
        if trip_update is None:
            continue
        trip = trip_update.get("trip", {})
        trip_id = str(trip.get("trip_id", ""))
        route_id = str(trip.get("route_id", ""))
        raw_direction = trip.get("direction_id")
        direction_id = str(raw_direction) if raw_direction is not None else None
        stop_time_updates = []
        for stu in trip_update.get("stop_time_update", []):
            arrival = stu.get("arrival") or {}
            departure = stu.get("departure") or {}
            stop_time_updates.append(
                {
                    "stop_id": str(stu.get("stop_id", "")),
                    "arrival_delay": _int_or_none(arrival.get("delay")),
                    "departure_delay": _int_or_none(departure.get("delay")),
                    "arrival_time": _int_or_none(arrival.get("time")),
                    "departure_time": _int_or_none(departure.get("time")),
                }
            )
        entities.append(
            {
                "trip_id": trip_id,
                "route_id": route_id,
                "direction_id": direction_id,
                "stop_time_updates": stop_time_updates,
            }
        )
    return entities


# --- Feed fetch ---


def _fetch_feed(api_key: str) -> dict:
    req = urllib.request.Request(
        _FEED_URL,
        headers={"x-api-key": api_key},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# --- Departure filtering ---


def _get_departures(
    entities: list[dict],
    stop_id: str,
    route_id: str,
    direction_id: str | None,
) -> list[dict]:
    now = datetime.now(_DUBLIN_TZ)
    results = []
    for entity in entities:
        if entity["route_id"] != route_id:
            continue
        if direction_id is not None and entity["direction_id"] != direction_id:
            continue
        for stu in entity["stop_time_updates"]:
            if stu["stop_id"] != stop_id:
                continue
            unix_ts = stu["departure_time"] or stu["arrival_time"]
            if unix_ts is None:
                continue
            dep_dt = datetime.fromtimestamp(unix_ts, tz=_DUBLIN_TZ)
            minutes = (dep_dt - now).total_seconds() / 60
            delay = stu["departure_delay"]
            results.append(
                {
                    "trip_id": entity["trip_id"],
                    "direction_id": entity["direction_id"],
                    "departure_time": dep_dt.strftime("%H:%M"),
                    "minutes_until": int(minutes),
                    "delay_minutes": round(delay / 60) if delay is not None else None,
                }
            )
    results.sort(key=lambda d: d["minutes_until"])
    return results


# --- Output ---


def _print_table(departures: list[dict], stop_id: str, route_id: str) -> None:
    print(f"\nRoute {route_id}  ·  Stop {stop_id}")
    print(f"Queried at {datetime.now(_DUBLIN_TZ).strftime('%H:%M:%S')} Dublin time\n")
    if not departures:
        print("  No upcoming departures found in the live feed.")
        return
    print(f"  {'Time':>5}  {'Min':>4}  {'Delay':>6}  {'Dir':>3}  Trip ID")
    print(f"  {'─' * 5}  {'─' * 4}  {'─' * 6}  {'─' * 3}  {'─' * 20}")
    for dep in departures:
        raw_delay = dep["delay_minutes"]
        delay = f"{raw_delay:+d}" if raw_delay is not None else "  n/a"
        direction = dep["direction_id"] if dep["direction_id"] is not None else "  -"
        print(
            f"  {dep['departure_time']:>5}  {dep['minutes_until']:>+4}  "
            f"{delay:>6}  {direction:>3}  {dep['trip_id']}"
        )
    print()


# --- Entry point ---


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query the NTA GTFS-RT feed for upcoming departures at a stop."
    )
    parser.add_argument("--api-key", required=True, help="NTA developer portal API key")
    parser.add_argument(
        "--stop-id", required=True, help="GTFS stop ID (e.g. 8220DB000836)"
    )
    parser.add_argument(
        "--route-id", required=True, help="Route short name (e.g. 46A, DART)"
    )
    parser.add_argument(
        "--direction",
        choices=["0", "1"],
        default=None,
        help="Filter by GTFS direction ID (0 or 1); omit for both directions",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="Output raw JSON"
    )
    args = parser.parse_args()

    try:
        payload = _fetch_feed(args.api_key)
    except Exception as exc:
        print(f"Error fetching feed: {exc}", file=sys.stderr)
        return 1

    entities = _parse_feed(payload)
    departures = _get_departures(entities, args.stop_id, args.route_id, args.direction)

    if args.as_json:
        print(json.dumps(departures, indent=2))
    else:
        _print_table(departures, args.stop_id, args.route_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
