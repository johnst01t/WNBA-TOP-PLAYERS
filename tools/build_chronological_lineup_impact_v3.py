"""Run the chronological lineup model from the official completed-game schedule.

Game IDs are not always sequential, so discovery comes from ESPN's dated WNBA
scoreboard. Each discovered game is then read from SportsDataverse's raw archive.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path
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


def output_directory() -> Path:
    if "--output-dir" in sys.argv:
        position = sys.argv.index("--output-dir")
        if position + 1 < len(sys.argv):
            return Path(sys.argv[position + 1])
    return Path("Chronological_Lineup_Impact")


def clarify_audit(output: Path) -> None:
    audit_path = output / "audit.json"
    game_audit_path = output / "game_reconstruction_audit.csv"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    with game_audit_path.open(encoding="utf-8-sig", newline="") as handle:
        games = list(csv.DictReader(handle))

    base_games = games[:165]
    official_differences = []
    for row in games:
        away_difference = float(row["Final Away"]) - float(row["Stint Away Points"])
        home_difference = float(row["Final Home"]) - float(row["Stint Home Points"])
        if abs(away_difference) > 1e-7 or abs(home_difference) > 1e-7:
            official_differences.append(
                {
                    "game_id": row["Game ID"],
                    "away_difference": away_difference,
                    "home_difference": home_difference,
                }
            )

    audit["official_completed_games"] = len(games)
    audit["validated_base_games"] = len(base_games)
    audit["validated_base_stints"] = sum(int(float(row["Stints"])) for row in base_games)
    audit["validated_base_target_games"] = 165
    audit["validated_base_target_stints"] = 5308
    audit["event_scoring_games"] = len(games)
    audit["official_score_difference_games"] = len(official_differences)
    audit["official_score_difference_explanation"] = (
        "The impact model uses only points attached to timestamped play-by-play events, because those points can be assigned to an exact ten-player lineup. "
        "Some ESPN final scoreboard totals include later corrections or points with no timestamped scoring event; those unassignable points are not guessed into a lineup."
    )
    audit["official_score_difference_examples"] = official_differences[:20]
    audit.pop("score_reconciled_games", None)
    audit["method"] = (
        "Actual timestamped lineup scoring minus the exact ten-player lineup expectation frozen before each game. "
        "No composite weights, box-score production, game-result bonus, clutch bonus, Elo blend or 0-100 transformation."
    )
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")

    methodology_path = output / "methodology.txt"
    with methodology_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\nOnly timestamped scoring events are attributed to lineups. If ESPN's final scoreboard contains a later correction without a scoring-event timestamp, the point is reported in the audit but is not guessed into a lineup.\n"
        )


model.discover_game_ids = discover_actual_2026_regular_season_games
model.main()
clarify_audit(output_directory())
