from __future__ import annotations

import argparse
import csv
import gzip
import io
import itertools
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

NEUTRAL_RATE = 80.0
PLAYER_PRIOR_MINUTES = 300.0
LEAGUE_PRIOR_MINUTES = 1200.0
PBP_GZ_URL = "https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-data/main/wnba/pbp/csv/play_by_play_2026.csv.gz"
RAW_GAME_URL = "https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-raw/main/wnba/json/final/{game_id}.json"


def fetch_bytes(url: str, attempts: int = 4) -> bytes | None:
    last = None
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": "chronological-lineup-impact/2.0"})
            with urlopen(req, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.25 * (attempt + 1))
    raise RuntimeError(f"Failed to download {url}: {last}")


def discover_game_ids() -> list[int]:
    raw = fetch_bytes(PBP_GZ_URL)
    if raw is None:
        raise RuntimeError("Processed 2026 play-by-play file was not available")
    text = gzip.decompress(raw).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    game_ids = set()
    for row in reader:
        value = row.get("game_id") or row.get("gameId")
        if value:
            try:
                game_ids.add(int(float(value)))
            except ValueError:
                pass
    return sorted(game_ids)


def play_elapsed(play: dict) -> float:
    period = int(play.get("period.number") or play.get("period") or 1)
    qrem = play.get("start.quarter_seconds_remaining")
    if qrem is None:
        qrem = float(play.get("clock.minutes") or 0) * 60 + float(play.get("clock.seconds") or 0)
    qrem = float(qrem)
    if period <= 4:
        return (period - 1) * 600.0 + (600.0 - qrem)
    return 2400.0 + (period - 5) * 300.0 + (300.0 - qrem)


def period_end_elapsed(period: int) -> float:
    if period <= 4:
        return period * 600.0
    return 2400.0 + (period - 4) * 300.0


def is_substitution(play: dict) -> bool:
    return "substitution" in str(play.get("type.text") or "").lower() or " enters the game for " in str(play.get("text") or "").lower()


def participants(play: dict) -> list[str]:
    out = []
    for index in range(3):
        raw = play.get(f"participants.{index}.athlete.id")
        if raw is not None:
            out.append(str(raw))
    return out


def athlete_metadata(data: dict):
    names: dict[str, str] = {}
    team_of: dict[str, str] = {}
    starters: dict[str, list[str]] = defaultdict(list)
    roster: dict[str, list[str]] = defaultdict(list)
    team_abbr: dict[str, str] = {}
    for team_block in (data.get("boxscore") or {}).get("players") or []:
        team = team_block.get("team") or {}
        tid = str(team.get("id"))
        team_abbr[tid] = str(team.get("abbreviation") or tid)
        groups = team_block.get("statistics") or []
        if not groups:
            continue
        for entry in groups[0].get("athletes") or []:
            athlete = entry.get("athlete") or {}
            raw_pid = athlete.get("id")
            if raw_pid is None:
                continue
            pid = str(raw_pid)
            names[pid] = str(athlete.get("displayName") or athlete.get("shortName") or pid)
            team_of[pid] = tid
            roster[tid].append(pid)
            if entry.get("starter"):
                starters[tid].append(pid)
    return names, team_of, starters, roster, team_abbr


def period_evidence(events: list[dict], tid: str, team_of: dict[str, str]):
    must_start: set[str] = set()
    cannot_start: set[str] = set()
    entered: set[str] = set()
    appeared: set[str] = set()
    for play in events:
        if is_substitution(play) and str(play.get("team.id")) == tid:
            incoming_raw = play.get("participants.0.athlete.id")
            outgoing_raw = play.get("participants.1.athlete.id")
            incoming = str(incoming_raw) if incoming_raw is not None else None
            outgoing = str(outgoing_raw) if outgoing_raw is not None else None
            if outgoing and outgoing not in entered:
                must_start.add(outgoing)
            if incoming and incoming not in appeared and incoming not in must_start:
                cannot_start.add(incoming)
            if incoming:
                entered.add(incoming)
            continue
        for pid in participants(play):
            if team_of.get(pid) != tid:
                continue
            appeared.add(pid)
            if pid not in entered:
                must_start.add(pid)
    return must_start, cannot_start


def candidate_penalty(candidate: tuple[str, ...], events: list[dict], tid: str, team_of: dict[str, str]) -> int:
    lineup = set(candidate)
    penalty = 0
    for play in events:
        if is_substitution(play) and str(play.get("team.id")) == tid:
            incoming_raw = play.get("participants.0.athlete.id")
            outgoing_raw = play.get("participants.1.athlete.id")
            incoming = str(incoming_raw) if incoming_raw is not None else None
            outgoing = str(outgoing_raw) if outgoing_raw is not None else None
            if outgoing not in lineup:
                penalty += 25
            if incoming in lineup:
                penalty += 25
            if outgoing in lineup:
                lineup.remove(outgoing)
            elif lineup:
                # Keep the simulation at five players after an invalid candidate transition.
                lineup.remove(sorted(lineup)[0])
            if incoming:
                lineup.add(incoming)
            if len(lineup) != 5:
                penalty += 50 * abs(len(lineup) - 5)
            continue
        for pid in participants(play):
            if team_of.get(pid) == tid and pid not in lineup:
                penalty += 10
    return penalty


def solve_period_start(
    events: list[dict],
    tid: str,
    roster: list[str],
    official_starters: list[str],
    carry: set[str] | None,
    period: int,
):
    team_of = {pid: tid for pid in roster}
    must, cannot = period_evidence(events, tid, team_of)
    roster_unique = list(dict.fromkeys(roster))
    if period == 1 and len(official_starters) == 5:
        official = tuple(sorted(official_starters))
        if candidate_penalty(official, events, tid, team_of) == 0:
            return set(official), {"must": len(must), "cannot": len(cannot), "candidates": 1, "penalty": 0, "source": "official starters"}

    if len(must) <= 5:
        eligible = [pid for pid in roster_unique if pid not in cannot or pid in must]
        remaining = [pid for pid in eligible if pid not in must]
        choose = 5 - len(must)
        combos = [tuple(sorted(tuple(must) + combo)) for combo in itertools.combinations(remaining, choose)] if choose >= 0 else []
    else:
        combos = [tuple(sorted(combo)) for combo in itertools.combinations(sorted(must), 5)]
    if not combos:
        combos = [tuple(sorted(combo)) for combo in itertools.combinations(roster_unique, 5)]

    best = None
    best_key = None
    carry = carry or set()
    official_set = set(official_starters)
    for combo in combos:
        penalty = candidate_penalty(combo, events, tid, team_of)
        overlap = len(set(combo) & carry)
        official_overlap = len(set(combo) & official_set)
        key = (penalty, -overlap, -official_overlap, combo)
        if best_key is None or key < best_key:
            best_key = key
            best = combo
    return set(best), {"must": len(must), "cannot": len(cannot), "candidates": len(combos), "penalty": best_key[0], "source": "period solver"}


def period_start_lineups(data: dict, team_of: dict[str, str], starters: dict[str, list[str]], roster: dict[str, list[str]]):
    plays = sorted(data.get("plays") or [], key=lambda p: int(p.get("game_play_number") or 0))
    periods = sorted(set(int(p.get("period.number") or p.get("period") or 1) for p in plays))
    team_ids = list(starters.keys())
    starts: dict[tuple[int, str], set[str]] = {}
    audit = []
    carry: dict[str, set[str]] = {tid: set(starters[tid]) for tid in team_ids}
    for period in periods:
        events = [p for p in plays if int(p.get("period.number") or p.get("period") or 1) == period]
        for tid in team_ids:
            start, info = solve_period_start(events, tid, roster[tid], starters[tid], carry.get(tid), period)
            starts[(period, tid)] = start
            info.update({"period": period, "team_id": tid, "start_count": len(start)})
            audit.append(info)
            lineup = set(start)
            for play in events:
                if not is_substitution(play) or str(play.get("team.id")) != tid:
                    continue
                inc_raw = play.get("participants.0.athlete.id")
                out_raw = play.get("participants.1.athlete.id")
                inc = str(inc_raw) if inc_raw is not None else None
                out = str(out_raw) if out_raw is not None else None
                if out in lineup:
                    lineup.remove(out)
                if inc:
                    lineup.add(inc)
            carry[tid] = lineup
    return starts, audit


@dataclass
class Stint:
    game_id: str
    date: str
    game_datetime: str
    home: str
    away: str
    start: float
    end: float
    minutes: float
    home_points: float
    away_points: float
    home_ids: tuple[str, ...]
    away_ids: tuple[str, ...]


def reconstruct_game(data: dict):
    plays = sorted(data.get("plays") or [], key=lambda p: int(p.get("game_play_number") or 0))
    names, team_of, starters, roster, team_abbr = athlete_metadata(data)
    first = plays[0]
    home_tid = str(first.get("homeTeamId"))
    away_tid = str(first.get("awayTeamId"))
    home = str(first.get("homeTeamAbbrev") or team_abbr.get(home_tid) or home_tid)
    away = str(first.get("awayTeamAbbrev") or team_abbr.get(away_tid) or away_tid)
    team_ids = [home_tid, away_tid]
    if any(len(starters.get(tid, [])) != 5 for tid in team_ids):
        raise ValueError(f"Invalid official starters in {data.get('gameId')}")
    starts, start_audit = period_start_lineups(data, team_of, starters, roster)

    current_period = int(first.get("period.number") or first.get("period") or 1)
    lineups = {tid: set(starts[(current_period, tid)]) for tid in team_ids}
    start_time = 0.0
    home_points = away_points = 0.0
    previous_home = previous_away = 0.0
    stints: list[Stint] = []
    errors = []
    game_id = str(data.get("gameId"))
    game_datetime = str(first.get("wallclock") or "")
    date = game_datetime[:10]

    def force_five(tid: str, period: int):
        desired = set(starts[(period, tid)])
        if len(lineups[tid]) != 5:
            lineups[tid] = desired

    def close(at: float):
        nonlocal start_time, home_points, away_points
        duration = max(0.0, at - start_time)
        if duration > 1e-8:
            for tid in team_ids:
                force_five(tid, current_period)
            stints.append(Stint(game_id, date, game_datetime, home, away, start_time, at, duration / 60.0, home_points, away_points, tuple(sorted(lineups[home_tid])), tuple(sorted(lineups[away_tid]))))
        start_time = at
        home_points = away_points = 0.0

    for play in plays:
        period = int(play.get("period.number") or play.get("period") or 1)
        at = play_elapsed(play)
        if period != current_period:
            close(period_end_elapsed(current_period))
            current_period = period
            lineups = {tid: set(starts[(period, tid)]) for tid in team_ids}
            start_time = at

        current_home = float(play.get("homeScore") or 0)
        current_away = float(play.get("awayScore") or 0)
        delta_home = current_home - previous_home
        delta_away = current_away - previous_away
        if abs(delta_home) <= 5:
            home_points += delta_home
        else:
            errors.append({"kind": "home score jump", "play": play.get("game_play_number"), "delta": delta_home})
        if abs(delta_away) <= 5:
            away_points += delta_away
        else:
            errors.append({"kind": "away score jump", "play": play.get("game_play_number"), "delta": delta_away})
        previous_home, previous_away = current_home, current_away

        if is_substitution(play):
            close(at)
            tid = str(play.get("team.id"))
            inc_raw = play.get("participants.0.athlete.id")
            out_raw = play.get("participants.1.athlete.id")
            inc = str(inc_raw) if inc_raw is not None else None
            out = str(out_raw) if out_raw is not None else None
            if tid not in lineups or inc is None or out is None:
                errors.append({"kind": "malformed substitution", "play": play.get("game_play_number")})
                continue
            if out not in lineups[tid] or inc in lineups[tid]:
                errors.append({"kind": "invalid substitution", "play": play.get("game_play_number"), "team": team_abbr.get(tid, tid), "incoming": inc, "outgoing": out, "lineup": sorted(lineups[tid])})
                # The independently solved period start is the source of truth; repair only to keep five.
                if out in lineups[tid]:
                    lineups[tid].remove(out)
                elif lineups[tid]:
                    removable = sorted(lineups[tid] - set(participants(play)))
                    lineups[tid].remove(removable[0] if removable else sorted(lineups[tid])[0])
                if inc not in lineups[tid]:
                    lineups[tid].add(inc)
                force_five(tid, period)
            else:
                lineups[tid].remove(out)
                lineups[tid].add(inc)

    max_period = max(int(p.get("period.number") or p.get("period") or 1) for p in plays)
    close(period_end_elapsed(max_period))
    final_home = float(plays[-1].get("homeScore") or 0)
    final_away = float(plays[-1].get("awayScore") or 0)
    return stints, names, {
        "game_id": game_id,
        "date": date,
        "home": home,
        "away": away,
        "stints": len(stints),
        "stint_home_points": sum(s.home_points for s in stints),
        "stint_away_points": sum(s.away_points for s in stints),
        "final_home": final_home,
        "final_away": final_away,
        "stint_minutes": sum(s.minutes for s in stints),
        "errors": errors,
        "period_solver": start_audit,
    }


def design(stint: Stint, player_index: dict[str, int], home_scoring: bool):
    indices = [0, 1]
    values = [1.0, 1.0 if home_scoring else -1.0]
    offense = stint.home_ids if home_scoring else stint.away_ids
    defense = stint.away_ids if home_scoring else stint.home_ids
    for pid in offense:
        indices.append(2 + player_index[pid])
        values.append(1.0)
    defense_offset = 2 + len(player_index)
    for pid in defense:
        indices.append(defense_offset + player_index[pid])
        values.append(-1.0)
    return np.asarray(indices, dtype=np.int64), np.asarray(values, dtype=float)


def predict(beta: np.ndarray, indices: np.ndarray, values: np.ndarray) -> float:
    return float(beta[indices] @ values)


def update_rls(beta: np.ndarray, covariance: np.ndarray, indices: np.ndarray, values: np.ndarray, response: float, weight: float):
    px = covariance[:, indices] @ values
    denominator = 1.0 / weight + float(values @ px[indices])
    error = response - float(beta[indices] @ values)
    gain = px / denominator
    beta += gain * error
    covariance -= np.outer(gain, px)
    covariance[:] = (covariance + covariance.T) * 0.5


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="Chronological_Lineup_Impact")
    args = parser.parse_args()
    output = Path(args.output_dir)
    raw_dir = output / "raw_json"
    raw_dir.mkdir(parents=True, exist_ok=True)

    discovered = discover_game_ids()
    games = []
    unavailable = []
    for game_id in discovered:
        path = raw_dir / f"{game_id}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = fetch_bytes(RAW_GAME_URL.format(game_id=game_id))
            if raw is None:
                unavailable.append(game_id)
                continue
            path.write_bytes(raw)
            data = json.loads(raw)
        plays = data.get("plays") or []
        if plays:
            games.append((str(plays[0].get("wallclock") or ""), game_id, data))
    games.sort(key=lambda item: (item[0], item[1]))

    all_names = {}
    reconstructed = []
    audits = []
    for _, _, data in games:
        stints, names, audit = reconstruct_game(data)
        all_names.update(names)
        reconstructed.append((data, stints))
        audits.append(audit)

    player_ids = sorted(all_names)
    pindex = {pid: i for i, pid in enumerate(player_ids)}
    dimension = 2 + 2 * len(player_ids)
    beta = np.zeros(dimension)
    prior_precision = np.full(dimension, PLAYER_PRIOR_MINUTES)
    prior_precision[:2] = LEAGUE_PRIOR_MINUTES
    covariance = np.diag(1.0 / prior_precision)
    minutes_to_date = defaultdict(float)
    games_to_date = defaultdict(int)

    player_game_rows = []
    lineup_rows = []
    game_rows = []
    player_history = []

    for sequence, (_, stints) in enumerate(reconstructed, 1):
        pregame = beta.copy()
        aggregation = defaultdict(lambda: {"minutes": 0.0, "team": "", "actual_for": 0.0, "expected_for": 0.0, "actual_against": 0.0, "expected_against": 0.0, "expected_net_minutes": 0.0})
        expected_home = expected_away = actual_home = actual_away = 0.0
        game_id = stints[0].game_id
        date = stints[0].date
        home = stints[0].home
        away = stints[0].away

        for stint_number, stint in enumerate(stints, 1):
            home_i, home_v = design(stint, pindex, True)
            away_i, away_v = design(stint, pindex, False)
            home_rate = NEUTRAL_RATE + predict(pregame, home_i, home_v)
            away_rate = NEUTRAL_RATE + predict(pregame, away_i, away_v)
            exp_home = home_rate * stint.minutes / 40.0
            exp_away = away_rate * stint.minutes / 40.0
            expected_home += exp_home
            expected_away += exp_away
            actual_home += stint.home_points
            actual_away += stint.away_points

            for pid in stint.home_ids:
                row = aggregation[pid]
                row["minutes"] += stint.minutes
                row["team"] = home
                row["actual_for"] += stint.home_points
                row["expected_for"] += exp_home
                row["actual_against"] += stint.away_points
                row["expected_against"] += exp_away
                row["expected_net_minutes"] += (home_rate - away_rate) * stint.minutes
            for pid in stint.away_ids:
                row = aggregation[pid]
                row["minutes"] += stint.minutes
                row["team"] = away
                row["actual_for"] += stint.away_points
                row["expected_for"] += exp_away
                row["actual_against"] += stint.home_points
                row["expected_against"] += exp_home
                row["expected_net_minutes"] += (away_rate - home_rate) * stint.minutes

            lineup_rows.append({
                "Game Sequence": sequence, "Game ID": game_id, "Date": date, "Home": home, "Away": away, "Stint": stint_number,
                "Start Sec": stint.start, "End Sec": stint.end, "Minutes": stint.minutes, "Home Points": stint.home_points, "Away Points": stint.away_points,
                "Expected Home Rate / 40": home_rate, "Expected Away Rate / 40": away_rate, "Expected Home Points": exp_home, "Expected Away Points": exp_away,
                "Home Offensive Residual": stint.home_points - exp_home, "Home Defensive Residual": exp_away - stint.away_points,
                "Home Net Residual": (stint.home_points - exp_home) + (exp_away - stint.away_points),
                "Away Offensive Residual": stint.away_points - exp_away, "Away Defensive Residual": exp_home - stint.home_points,
                "Away Net Residual": (stint.away_points - exp_away) + (exp_home - stint.home_points),
                **{f"Home {i+1}": all_names.get(pid, pid) for i, pid in enumerate(stint.home_ids)},
                **{f"Away {i+1}": all_names.get(pid, pid) for i, pid in enumerate(stint.away_ids)},
            })

        # Strict chronology: update only after every expectation for the game is frozen.
        for stint in stints:
            for home_scoring, points in ((True, stint.home_points), (False, stint.away_points)):
                indices, values = design(stint, pindex, home_scoring)
                response = points / stint.minutes * 40.0 - NEUTRAL_RATE
                update_rls(beta, covariance, indices, values, response, stint.minutes)

        for pid, aggregate in aggregation.items():
            minutes = aggregate["minutes"]
            off_adjusted = aggregate["actual_for"] - aggregate["expected_for"]
            def_adjusted = aggregate["expected_against"] - aggregate["actual_against"]
            net_adjusted = off_adjusted + def_adjusted
            off_index = 2 + pindex[pid]
            def_index = 2 + len(pindex) + pindex[pid]
            pre_off, pre_def = pregame[off_index], pregame[def_index]
            post_off, post_def = beta[off_index], beta[def_index]
            minutes_to_date[pid] += minutes
            games_to_date[pid] += 1
            reliability = minutes_to_date[pid] / (minutes_to_date[pid] + PLAYER_PRIOR_MINUTES)
            player_game_rows.append({
                "Game Sequence": sequence, "Game ID": game_id, "Date": date, "Player": all_names.get(pid, pid), "Player ID": pid,
                "Team": aggregate["team"], "Opponent": away if aggregate["team"] == home else home, "Minutes": minutes,
                "Pregame Player Offence / 40": pre_off, "Pregame Player Defence / 40": pre_def, "Pregame Player Net / 40": pre_off + pre_def,
                "Expected Team Points While On": aggregate["expected_for"], "Actual Team Points While On": aggregate["actual_for"], "Offensive Adjusted Points": off_adjusted,
                "Expected Opponent Points While On": aggregate["expected_against"], "Actual Opponent Points While On": aggregate["actual_against"], "Defensive Adjusted Points": def_adjusted,
                "Net Adjusted Points": net_adjusted, "Net Adjusted Points / 40": net_adjusted / minutes * 40.0,
                "Expected Lineup Net / 40": aggregate["expected_net_minutes"] / minutes,
                "Actual Lineup Net / 40": (aggregate["actual_for"] - aggregate["actual_against"]) / minutes * 40.0,
                "Postgame Player Offence / 40": post_off, "Postgame Player Defence / 40": post_def, "Postgame Player Net / 40": post_off + post_def,
                "Postgame Rating Change": (post_off + post_def) - (pre_off + pre_def), "Games To Date": games_to_date[pid], "Minutes To Date": minutes_to_date[pid], "Reliability": reliability,
            })
            player_history.append({
                "Game Sequence": sequence, "Game ID": game_id, "Date": date, "Player": all_names.get(pid, pid), "Player ID": pid, "Team": aggregate["team"],
                "Game Minutes": minutes, "Games To Date": games_to_date[pid], "Minutes To Date": minutes_to_date[pid],
                "Pregame Offence": pre_off, "Postgame Offence": post_off, "Offence Change": post_off - pre_off,
                "Pregame Defence": pre_def, "Postgame Defence": post_def, "Defence Change": post_def - pre_def,
                "Pregame Net": pre_off + pre_def, "Postgame Net": post_off + post_def, "Net Change": (post_off + post_def) - (pre_off + pre_def), "Reliability": reliability,
            })

        game_rows.append({
            "Game Sequence": sequence, "Game ID": game_id, "Date": date, "Away": away, "Home": home,
            "Actual Away": actual_away, "Actual Home": actual_home, "Expected Away": expected_away, "Expected Home": expected_home,
            "Away Residual": actual_away - expected_away, "Home Residual": actual_home - expected_home,
            "Actual Home Margin": actual_home - actual_away, "Expected Home Margin": expected_home - expected_away,
            "Margin vs Expectation": (actual_home - actual_away) - (expected_home - expected_away),
            "Pregame Baseline / 40": NEUTRAL_RATE + pregame[0], "Pregame Home Court / 40": pregame[1],
            "Postgame Baseline / 40": NEUTRAL_RATE + beta[0], "Postgame Home Court / 40": beta[1],
        })

    player_game_rows.sort(key=lambda row: row["Net Adjusted Points"], reverse=True)
    for rank, row in enumerate(player_game_rows, 1):
        row["Total Impact Rank"] = rank
    rate_rows = sorted((row for row in player_game_rows if row["Minutes"] >= 10), key=lambda row: row["Net Adjusted Points / 40"], reverse=True)
    rate_lookup = {(row["Game ID"], row["Player ID"]): rank for rank, row in enumerate(rate_rows, 1)}
    for row in player_game_rows:
        row["Rate Rank (10+ Min)"] = rate_lookup.get((row["Game ID"], row["Player ID"]), "")

    final_players = []
    for pid in player_ids:
        off = beta[2 + pindex[pid]]
        deff = beta[2 + len(pindex) + pindex[pid]]
        minutes = minutes_to_date[pid]
        final_players.append({"Player": all_names.get(pid, pid), "Player ID": pid, "Games": games_to_date[pid], "Minutes": minutes, "Offence / 40": off, "Defence / 40": deff, "Net / 40": off + deff, "Reliability": minutes / (minutes + PLAYER_PRIOR_MINUTES) if minutes else 0.0})
    final_players.sort(key=lambda row: row["Net / 40"], reverse=True)
    for rank, row in enumerate(final_players, 1):
        row["Net Rank"] = rank

    player_game_fields = ["Total Impact Rank", "Rate Rank (10+ Min)", "Game Sequence", "Game ID", "Date", "Player", "Player ID", "Team", "Opponent", "Minutes", "Pregame Player Offence / 40", "Pregame Player Defence / 40", "Pregame Player Net / 40", "Expected Team Points While On", "Actual Team Points While On", "Offensive Adjusted Points", "Expected Opponent Points While On", "Actual Opponent Points While On", "Defensive Adjusted Points", "Net Adjusted Points", "Net Adjusted Points / 40", "Expected Lineup Net / 40", "Actual Lineup Net / 40", "Postgame Player Offence / 40", "Postgame Player Defence / 40", "Postgame Player Net / 40", "Postgame Rating Change", "Games To Date", "Minutes To Date", "Reliability"]
    write_csv(output / "player_game_impact.csv", player_game_rows, player_game_fields)
    write_csv(output / "lineup_expectations.csv", lineup_rows, list(lineup_rows[0]))
    write_csv(output / "player_history.csv", player_history, list(player_history[0]))
    write_csv(output / "game_expectations.csv", game_rows, list(game_rows[0]))
    write_csv(output / "final_player_ratings.csv", final_players, ["Net Rank", "Player", "Player ID", "Games", "Minutes", "Offence / 40", "Defence / 40", "Net / 40", "Reliability"])

    audit_rows = []
    for audit in audits:
        period_info = audit["period_solver"]
        audit_rows.append({
            "Game ID": audit["game_id"], "Date": audit["date"], "Away": audit["away"], "Home": audit["home"], "Stints": audit["stints"],
            "Stint Away Points": audit["stint_away_points"], "Final Away": audit["final_away"], "Stint Home Points": audit["stint_home_points"], "Final Home": audit["final_home"],
            "Total Stint Minutes": audit["stint_minutes"], "Substitution Errors": len(audit["errors"]),
            "Period Teams With Exact Five Direct Clues": sum(1 for row in period_info if row["must"] == 5),
            "Period Teams With Zero Solver Penalty": sum(1 for row in period_info if row["penalty"] == 0),
        })
    write_csv(output / "game_reconstruction_audit.csv", audit_rows, list(audit_rows[0]))

    through_cutoff = [audit for audit in audits if audit["date"] <= "2026-07-09"]
    audit_summary = {
        "processed_csv_game_ids": len(discovered),
        "raw_games_available": len(games),
        "unavailable_game_ids": unavailable,
        "first_date": audits[0]["date"],
        "last_date": audits[-1]["date"],
        "players": len(player_ids),
        "stints": len(lineup_rows),
        "player_games": len(player_game_rows),
        "games_through_2026_07_09": len(through_cutoff),
        "stints_through_2026_07_09": sum(row["stints"] for row in through_cutoff),
        "target_old_games": 165,
        "target_old_stints": 5308,
        "score_reconciled_games": sum(1 for row in audits if abs(row["stint_home_points"] - row["final_home"]) < 1e-7 and abs(row["stint_away_points"] - row["final_away"]) < 1e-7),
        "games_with_substitution_errors": sum(1 for row in audits if row["errors"]),
        "total_substitution_errors": sum(len(row["errors"]) for row in audits),
        "period_team_units": sum(len(row["period_solver"]) for row in audits),
        "period_team_units_with_exact_five_direct_clues": sum(sum(1 for p in row["period_solver"] if p["must"] == 5) for row in audits),
        "period_team_units_with_zero_solver_penalty": sum(sum(1 for p in row["period_solver"] if p["penalty"] == 0) for row in audits),
        "final_baseline_per_40": NEUTRAL_RATE + beta[0],
        "final_home_court_per_40": beta[1],
        "method": "Actual lineup scoring minus the exact ten-player lineup expectation frozen before each game. No composite weights, box-score production, game-result bonus, clutch bonus, Elo blend or 0-100 transformation.",
        "limitation": "All players sharing a stint share that stint's residual. Individual separation comes from different substitution patterns and the chronological regularized model; one fully shared stint cannot prove isolated causality.",
    }
    (output / "audit.json").write_text(json.dumps(audit_summary, indent=2), encoding="utf-8")
    (output / "methodology.txt").write_text("\n".join([
        "Offensive adjusted points = actual team points while the player was on court minus expected team points from the exact ten-player lineup before the game.",
        "Defensive adjusted points = expected opponent points while the player was on court minus actual opponent points.",
        "Net adjusted points = offensive adjusted points + defensive adjusted points.",
        "Player and lineup strengths are frozen before each game and updated only after that game is complete.",
        "No box-score production, win bonus, margin bonus, clutch bonus, opponent Elo blend, arbitrary component weights or 0-100 rescaling is used.",
        "Net adjusted points is the primary total game-impact measure. Net adjusted points per 40 is the rate measure and is separately ranked only for players with at least 10 minutes.",
        "Limitation: teammates sharing the same stint receive the same stint residual. The model controls for the other nine players chronologically, but a single shared stint cannot establish isolated causality.",
    ]), encoding="utf-8")
    print(json.dumps(audit_summary, indent=2))
    print("TOP 20 RAW NET ADJUSTED POINT GAMES")
    for row in player_game_rows[:20]:
        print(row["Total Impact Rank"], row["Date"], row["Player"], row["Team"], row["Opponent"], round(row["Net Adjusted Points"], 3), round(row["Net Adjusted Points / 40"], 3), round(row["Minutes"], 2))


if __name__ == "__main__":
    main()
