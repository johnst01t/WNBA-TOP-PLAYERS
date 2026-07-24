"""Run the validated chronological lineup model using raw-game discovery.

The processed 2026 season CSV is occasionally unpublished. This wrapper discovers
all actual 2026 regular-season games directly from the public raw ESPN archive,
then delegates every calculation to the v2 model.
"""
from __future__ import annotations

import json

import build_chronological_lineup_impact_v2 as model


def discover_actual_2026_regular_season_games() -> list[int]:
    game_ids: list[int] = []
    # Includes the one early game ID outside the 401856890..401857091 span.
    for game_id in range(401856800, 401857092):
        raw = model.fetch_bytes(model.RAW_GAME_URL.format(game_id=game_id), attempts=2)
        if raw is None:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        plays = data.get("plays") or []
        if not plays:
            continue
        first = plays[0]
        if int(first.get("season") or 0) == 2026 and int(first.get("seasonType") or 0) == 2:
            game_ids.append(game_id)
    return sorted(game_ids)


model.discover_game_ids = discover_actual_2026_regular_season_games
model.main()
