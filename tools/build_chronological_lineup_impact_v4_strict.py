"""Strict clean-only chronological lineup impact.

Only games whose every period-team starting unit is directly identified with a
zero-penalty substitution sequence are allowed into training or rankings.
Excluded games are published in the audit rather than repaired or guessed.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import build_chronological_lineup_impact_v2 as model

EXCLUDED: list[dict] = []
OFFICIAL_IDS: list[int] = []


def official_completed_ids() -> list[int]:
    ids: set[int] = set()
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
            if int(season.get("type") or 0) == 2 and status.get("completed") and event.get("id") is not None:
                ids.add(int(event["id"]))
        day += dt.timedelta(days=1)
    return sorted(ids)


def discover_clean_games() -> list[int]:
    global OFFICIAL_IDS
    OFFICIAL_IDS = official_completed_ids()
    clean: list[int] = []
    for game_id in OFFICIAL_IDS:
        raw = model.fetch_bytes(model.RAW_GAME_URL.format(game_id=game_id))
        if raw is None:
            EXCLUDED.append({"game_id": game_id, "reason": "raw game unavailable"})
            continue
        data = json.loads(raw)
        try:
            _, _, audit = model.reconstruct_game(data)
        except Exception as exc:  # noqa: BLE001
            EXCLUDED.append({"game_id": game_id, "reason": "reconstruction failure", "detail": str(exc)})
            continue
        period_units = audit["period_solver"]
        ambiguous = [
            {
                "period": row["period"],
                "team_id": row["team_id"],
                "direct_starters": row["must"],
                "solver_penalty": row["penalty"],
            }
            for row in period_units
            if row["must"] != 5 or row["penalty"] != 0
        ]
        if audit["errors"] or ambiguous:
            EXCLUDED.append(
                {
                    "game_id": game_id,
                    "date": audit["date"],
                    "away": audit["away"],
                    "home": audit["home"],
                    "reason": "lineup evidence not fully clean",
                    "substitution_errors": audit["errors"],
                    "ambiguous_period_teams": ambiguous,
                }
            )
            continue
        clean.append(game_id)
    return clean


def output_directory() -> Path:
    if "--output-dir" in sys.argv:
        index = sys.argv.index("--output-dir")
        if index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1])
    return Path("Chronological_Lineup_Impact")


def finalize_audit(output: Path) -> None:
    audit_path = output / "audit.json"
    game_audit_path = output / "game_reconstruction_audit.csv"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    with game_audit_path.open(encoding="utf-8-sig", newline="") as handle:
        games = list(csv.DictReader(handle))

    score_differences = []
    for row in games:
        away_difference = float(row["Final Away"]) - float(row["Stint Away Points"])
        home_difference = float(row["Final Home"]) - float(row["Stint Home Points"])
        if abs(away_difference) > 1e-7 or abs(home_difference) > 1e-7:
            score_differences.append({"game_id": row["Game ID"], "away_difference": away_difference, "home_difference": home_difference})

    audit.clear()
    audit.update(
        {
            "official_completed_games": len(OFFICIAL_IDS),
            "clean_games_used": len(games),
            "games_excluded": len(EXCLUDED),
            "excluded_games": EXCLUDED,
            "first_date": games[0]["Date"] if games else None,
            "last_date": games[-1]["Date"] if games else None,
            "players": None,
            "stints": sum(int(float(row["Stints"])) for row in games),
            "all_games_zero_substitution_errors": all(int(float(row["Substitution Errors"])) == 0 for row in games),
            "all_period_team_units_have_exact_five_direct_clues": all(
                int(float(row["Period Teams With Exact Five Direct Clues"])) == int(float(row["Period Teams With Zero Solver Penalty"]))
                for row in games
            ),
            "official_score_difference_games": len(score_differences),
            "official_score_difference_explanation": (
                "Only points attached to timestamped play-by-play scoring events are attributed to lineups. "
                "Later scoreboard corrections without an event timestamp are audited but never guessed into a lineup."
            ),
            "official_score_difference_examples": score_differences[:20],
            "method": (
                "Actual timestamped scoring by the player's exact five-player unit minus the pregame expectation for the exact ten players on court. "
                "Expectations use only prior clean games. No box score, result bonus, clutch bonus, Elo blend, composite weights or 0-100 score."
            ),
            "primary_measure": "Net Adjusted Points",
            "rate_measure": "Net Adjusted Points / 40, ranked only for games with at least 10 minutes",
            "causality_limit": (
                "Every teammate sharing a stint shares that stint residual. Different substitution patterns allow separation across stints, "
                "but one shared stint cannot prove which individual caused the result."
            ),
        }
    )

    # Recover player/stint totals from generated files.
    player_game_path = output / "player_game_impact.csv"
    with player_game_path.open(encoding="utf-8-sig", newline="") as handle:
        player_games = list(csv.DictReader(handle))
    audit["player_games"] = len(player_games)
    audit["players"] = len({row["Player ID"] for row in player_games})
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    (output / "excluded_games.json").write_text(json.dumps(EXCLUDED, indent=2), encoding="utf-8")

    methodology = output / "methodology.txt"
    methodology.write_text(
        "\n".join(
            [
                "STRICT CLEAN-ONLY CHRONOLOGICAL LINEUP IMPACT",
                "Offensive Adjusted Points = actual timestamped team points while the player was on court minus expected team points for the exact ten-player lineup before the game.",
                "Defensive Adjusted Points = expected opponent points for the exact ten-player lineup minus actual timestamped opponent points.",
                "Net Adjusted Points = Offensive Adjusted Points + Defensive Adjusted Points.",
                "Every expectation is frozen before the game and learned only from earlier clean games.",
                "Games with any ambiguous period lineup, non-zero solver penalty or substitution inconsistency are excluded from both training and ranking.",
                "No box-score production, win bonus, margin bonus, clutch bonus, Elo component, composite weighting or 0-100 rescaling is used.",
                "Only timestamped scoring events are assigned to lineups. Un-timestamped later scoreboard corrections are audited and left unassigned.",
                "Net Adjusted Points is the primary total game-impact measure. Net Adjusted Points / 40 is a separate rate measure for 10+ minute samples.",
                "Causality limit: all players sharing a stint share its residual; one shared stint cannot isolate which teammate caused the outcome.",
            ]
        ),
        encoding="utf-8",
    )


model.discover_game_ids = discover_clean_games
model.main()
finalize_audit(output_directory())
