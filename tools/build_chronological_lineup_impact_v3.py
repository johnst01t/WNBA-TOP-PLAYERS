"""Run the chronological lineup model from the official completed-game schedule.

Game IDs are not always sequential, so discovery comes from ESPN's dated WNBA
scoreboard. Each discovered game is then read from SportsDataverse's raw archive.
"""
from __future__ import annotations

import datetime as dt
import json
from urllib.request import Request, urlopen

import build_chronological_lineup_impact_v2 as model


def discover_actual_2026_regular_season_games() -> list[int]:
    game_ids: set[int] = set()
    day = dt.date(2026, 5, 1)
    end = dt.date(2026, 7, 24)
    while day <= end:
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
            f"?dates={day:%Y%m%d}&limit=100"
        )
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
        except Exception:  # noqa: BLE001
            day += dt.timedelta(days=1)
            continue
        for event in data.get("events", []):
            season = event.get("season") or {}
            competition = (event.get("competitions") or [{}])[0]
            status = ((competition.get("status") or {}).get("type") or {})
            if int(season.get("type") or 0) != 2 or not status.get("completed"):
                continue
            raw_id = event.get("id")
            if raw_id is not None:
                game_ids.add(int(raw_id))
        day += dt.timedelta(days=1)
    return sorted(game_ids)


model.discover_game_ids = discover_actual_2026_regular_season_games
model.main()
