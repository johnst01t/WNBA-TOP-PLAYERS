from __future__ import annotations

import argparse
import csv
import json
import math
import re
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


def get_json(url: str, attempts: int = 4):
    last = None
    for i in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": "WNBA-lineup-impact/1.0"})
            with urlopen(req, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"Unable to download {url}: {last}")


def play_elapsed(play: dict) -> float:
    period = int(play.get("period.number") or play.get("period") or 1)
    qrem = play.get("start.quarter_seconds_remaining")
    if qrem is None:
        mins = float(play.get("clock.minutes") or 0)
        secs = float(play.get("clock.seconds") or 0)
        qrem = mins * 60 + secs
    qrem = float(qrem)
    if period <= 4:
        return (period - 1) * 600.0 + (600.0 - qrem)
    return 2400.0 + (period - 5) * 300.0 + (300.0 - qrem)


def game_duration(plays: list[dict]) -> float:
    max_period = max(int(p.get("period.number") or p.get("period") or 1) for p in plays)
    return 2400.0 + max(0, max_period - 4) * 300.0


def athlete_maps(data: dict):
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
            pid = str(athlete.get("id"))
            if not pid or pid == "None":
                continue
            name = str(athlete.get("displayName") or athlete.get("shortName") or pid)
            names[pid] = name
            team_of[pid] = tid
            roster[tid].append(pid)
            if entry.get("starter"):
                starters[tid].append(pid)
    for p in data.get("plays") or []:
        tid = p.get("team.id")
        tid = str(tid) if tid is not None else None
        for k in range(3):
            raw = p.get(f"participants.{k}.athlete.id")
            if raw is None:
                continue
            pid = str(raw)
            if pid not in names:
                names[pid] = pid
            if tid and pid not in team_of:
                team_of[pid] = tid
    return names, team_of, starters, roster, team_abbr


def period_direct_clues(plays: list[dict], team_ids: list[str], team_of: dict[str, str]):
    """Players demonstrably active before their first substitution in each period."""
    clues: dict[tuple[int, str], set[str]] = defaultdict(set)
    first_sub_seen: set[tuple[int, str]] = set()
    for p in sorted(plays, key=lambda x: int(x.get("game_play_number") or 0)):
        period = int(p.get("period.number") or p.get("period") or 1)
        typ = str(p.get("type.text") or "").lower()
        tid_raw = p.get("team.id")
        tid = str(tid_raw) if tid_raw is not None else None
        is_sub = "substitution" in typ or " enters the game for " in str(p.get("text") or "").lower()
        if is_sub and tid in team_ids:
            incoming = p.get("participants.0.athlete.id")
            outgoing = p.get("participants.1.athlete.id")
            if outgoing is not None:
                clues[(period, tid)].add(str(outgoing))
            first_sub_seen.add((period, tid))
            continue
        for k in range(3):
            raw = p.get(f"participants.{k}.athlete.id")
            if raw is None:
                continue
            pid = str(raw)
            ptid = team_of.get(pid)
            if ptid in team_ids and (period, ptid) not in first_sub_seen:
                clues[(period, ptid)].add(pid)
    return clues


def repair_period_lineup(
    current: set[str],
    period: int,
    tid: str,
    clues: dict[tuple[int, str], set[str]],
    roster: dict[str, list[str]],
) -> set[str]:
    direct = set(clues.get((period, tid), set()))
    if len(direct) == 5:
        return direct
    keep = [p for p in current if p in roster.get(tid, [])]
    result = list(dict.fromkeys(list(direct) + keep))
    for p in roster.get(tid, []):
        if p not in result:
            result.append(p)
        if len(result) >= 5:
            break
    return set(result[:5])


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
    plays = sorted(data.get("plays") or [], key=lambda x: int(x.get("game_play_number") or 0))
    if not plays:
        raise ValueError("No plays")
    names, team_of, starters, roster, team_abbr = athlete_maps(data)
    first = plays[0]
    home_tid = str(first.get("homeTeamId"))
    away_tid = str(first.get("awayTeamId"))
    home = str(first.get("homeTeamAbbrev") or team_abbr.get(home_tid) or home_tid)
    away = str(first.get("awayTeamAbbrev") or team_abbr.get(away_tid) or away_tid)
    if len(starters.get(home_tid, [])) != 5 or len(starters.get(away_tid, [])) != 5:
        raise ValueError(f"Starter count {home}:{len(starters.get(home_tid, []))}, {away}:{len(starters.get(away_tid, []))}")
    lineups = {home_tid: set(starters[home_tid]), away_tid: set(starters[away_tid])}
    clues = period_direct_clues(plays, [home_tid, away_tid], team_of)
    current_period = 1
    stint_start = 0.0
    home_pts = away_pts = 0.0
    prev_home = prev_away = 0.0
    stints: list[Stint] = []
    errors: list[dict] = []
    repaired_periods: list[dict] = []
    game_id = str(data.get("gameId"))
    dt = str(first.get("wallclock") or "")
    date = dt[:10]

    def close_stint(at: float):
        nonlocal stint_start, home_pts, away_pts
        duration = max(0.0, at - stint_start)
        if duration > 1e-9 and len(lineups[home_tid]) == 5 and len(lineups[away_tid]) == 5:
            stints.append(
                Stint(
                    game_id=game_id,
                    date=date,
                    game_datetime=dt,
                    home=home,
                    away=away,
                    start=stint_start,
                    end=at,
                    minutes=duration / 60.0,
                    home_points=home_pts,
                    away_points=away_pts,
                    home_ids=tuple(sorted(lineups[home_tid])),
                    away_ids=tuple(sorted(lineups[away_tid])),
                )
            )
        elif duration > 1e-9:
            errors.append({"kind": "invalid_lineup_at_close", "at": at, "home_count": len(lineups[home_tid]), "away_count": len(lineups[away_tid])})
        stint_start = at
        home_pts = away_pts = 0.0

    for p in plays:
        period = int(p.get("period.number") or p.get("period") or 1)
        at = play_elapsed(p)
        if period != current_period:
            close_stint((current_period * 600.0) if current_period <= 4 else (2400.0 + (current_period - 4) * 300.0))
            current_period = period
            for tid in [home_tid, away_tid]:
                repaired = repair_period_lineup(lineups[tid], period, tid, clues, roster)
                if repaired != lineups[tid]:
                    repaired_periods.append({"period": period, "team": team_abbr.get(tid, tid), "before": sorted(lineups[tid]), "after": sorted(repaired), "direct": sorted(clues.get((period, tid), set()))})
                    lineups[tid] = repaired
            stint_start = at

        typ = str(p.get("type.text") or "").lower()
        text = str(p.get("text") or "").lower()
        is_sub = "substitution" in typ or " enters the game for " in text
        if is_sub:
            close_stint(at)
            tid_raw = p.get("team.id")
            tid = str(tid_raw) if tid_raw is not None else None
            incoming_raw = p.get("participants.0.athlete.id")
            outgoing_raw = p.get("participants.1.athlete.id")
            incoming = str(incoming_raw) if incoming_raw is not None else None
            outgoing = str(outgoing_raw) if outgoing_raw is not None else None
            if tid not in lineups or incoming is None or outgoing is None:
                errors.append({"kind": "malformed_sub", "play": p.get("game_play_number"), "text": p.get("text")})
                continue
            if outgoing not in lineups[tid] or incoming in lineups[tid]:
                repaired = repair_period_lineup(lineups[tid], period, tid, clues, roster)
                if repaired != lineups[tid]:
                    repaired_periods.append({"period": period, "team": team_abbr.get(tid, tid), "before": sorted(lineups[tid]), "after": sorted(repaired), "reason": "substitution repair"})
                    lineups[tid] = repaired
            if outgoing in lineups[tid] and incoming not in lineups[tid]:
                lineups[tid].remove(outgoing)
                lineups[tid].add(incoming)
            else:
                errors.append({"kind": "invalid_sub", "play": p.get("game_play_number"), "team": team_abbr.get(tid, tid), "incoming": incoming, "outgoing": outgoing, "lineup": sorted(lineups[tid]), "text": p.get("text")})
            continue

        hs = float(p.get("homeScore") or 0)
        aws = float(p.get("awayScore") or 0)
        dh = hs - prev_home
        da = aws - prev_away
        if abs(dh) <= 5:
            home_pts += dh
        else:
            errors.append({"kind": "large_home_score_change", "play": p.get("game_play_number"), "change": dh})
        if abs(da) <= 5:
            away_pts += da
        else:
            errors.append({"kind": "large_away_score_change", "play": p.get("game_play_number"), "change": da})
        prev_home, prev_away = hs, aws

    close_stint(game_duration(plays))
    final_home = max(float(p.get("homeScore") or 0) for p in plays)
    final_away = max(float(p.get("awayScore") or 0) for p in plays)
    audit = {
        "game_id": game_id,
        "date": date,
        "home": home,
        "away": away,
        "stints": len(stints),
        "stint_home_points": sum(s.home_points for s in stints),
        "stint_away_points": sum(s.away_points for s in stints),
        "final_home": final_home,
        "final_away": final_away,
        "minutes": sum(s.minutes for s in stints),
        "errors": errors,
        "repairs": repaired_periods,
    }
    return stints, names, audit


def design_indices(stint: Stint, player_index: dict[str, int], home_scoring: bool):
    idx = [0, 1]
    val = [1.0, 1.0 if home_scoring else -1.0]
    if home_scoring:
        offense, defense = stint.home_ids, stint.away_ids
    else:
        offense, defense = stint.away_ids, stint.home_ids
    for pid in offense:
        idx.append(2 + player_index[pid])
        val.append(1.0)
    offset = 2 + len(player_index)
    for pid in defense:
        idx.append(offset + player_index[pid])
        val.append(-1.0)
    return np.asarray(idx, dtype=np.int64), np.asarray(val, dtype=float)


def sparse_dot(beta: np.ndarray, idx: np.ndarray, val: np.ndarray) -> float:
    return float(beta[idx] @ val)


def rls_update(beta: np.ndarray, cov: np.ndarray, idx: np.ndarray, val: np.ndarray, y: float, weight: float):
    if weight <= 0:
        return
    px = cov[:, idx] @ val
    denom = 1.0 / weight + float(val @ px[idx])
    if denom <= 1e-12:
        return
    error = y - float(beta[idx] @ val)
    gain = px / denom
    beta += gain * error
    cov -= np.outer(gain, px)
    cov[:] = (cov + cov.T) * 0.5


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-id", type=int, default=401856890)
    parser.add_argument("--end-id", type=int, default=401857091)
    parser.add_argument("--output-dir", default="chronological_lineup_impact")
    args = parser.parse_args()
    out = Path(args.output_dir)
    raw_dir = out / "raw_json"
    raw_dir.mkdir(parents=True, exist_ok=True)

    games = []
    missing = []
    for gid in range(args.start_id, args.end_id + 1):
        path = raw_dir / f"{gid}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = get_json(f"https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-raw/main/wnba/json/final/{gid}.json")
            if data is None:
                missing.append(gid)
                continue
            path.write_text(json.dumps(data), encoding="utf-8")
        plays = data.get("plays") or []
        if not plays:
            continue
        first = plays[0]
        if int(first.get("seasonType") or 0) != 2:
            continue
        dt = str(first.get("wallclock") or "")
        games.append((dt, gid, data))
    games.sort(key=lambda x: (x[0], x[1]))

    all_names: dict[str, str] = {}
    reconstructed = []
    audits = []
    for _, _, data in games:
        stints, names, audit = reconstruct_game(data)
        reconstructed.append((data, stints))
        all_names.update(names)
        audits.append(audit)

    player_ids = sorted(all_names)
    pidx = {pid: i for i, pid in enumerate(player_ids)}
    n = 2 + 2 * len(player_ids)
    beta = np.zeros(n, dtype=float)
    prior = np.full(n, PLAYER_PRIOR_MINUTES, dtype=float)
    prior[0:2] = LEAGUE_PRIOR_MINUTES
    cov = np.diag(1.0 / prior)
    cumulative_minutes = defaultdict(float)
    games_to_date = defaultdict(int)

    lineup_rows = []
    player_game_rows = []
    player_history_rows = []
    game_rows = []

    for game_sequence, (data, stints) in enumerate(reconstructed, 1):
        pre_beta = beta.copy()
        player_agg = defaultdict(lambda: {
            "minutes": 0.0,
            "off_actual": 0.0,
            "off_expected": 0.0,
            "opp_actual": 0.0,
            "opp_expected": 0.0,
            "expected_net_minutes": 0.0,
            "team": "",
        })
        game_expected_home = game_expected_away = 0.0
        game_actual_home = game_actual_away = 0.0
        date = stints[0].date if stints else ""
        gid = stints[0].game_id if stints else str(data.get("gameId"))
        home = stints[0].home if stints else ""
        away = stints[0].away if stints else ""

        for stint_number, stint in enumerate(stints, 1):
            hi, hv = design_indices(stint, pidx, True)
            ai, av = design_indices(stint, pidx, False)
            home_rate = NEUTRAL_RATE + sparse_dot(pre_beta, hi, hv)
            away_rate = NEUTRAL_RATE + sparse_dot(pre_beta, ai, av)
            exp_home = home_rate * stint.minutes / 40.0
            exp_away = away_rate * stint.minutes / 40.0
            home_off_resid = stint.home_points - exp_home
            home_def_resid = exp_away - stint.away_points
            away_off_resid = stint.away_points - exp_away
            away_def_resid = exp_home - stint.home_points
            game_expected_home += exp_home
            game_expected_away += exp_away
            game_actual_home += stint.home_points
            game_actual_away += stint.away_points

            for pid in stint.home_ids:
                a = player_agg[pid]
                a["minutes"] += stint.minutes
                a["off_actual"] += stint.home_points
                a["off_expected"] += exp_home
                a["opp_actual"] += stint.away_points
                a["opp_expected"] += exp_away
                a["expected_net_minutes"] += (home_rate - away_rate) * stint.minutes
                a["team"] = home
            for pid in stint.away_ids:
                a = player_agg[pid]
                a["minutes"] += stint.minutes
                a["off_actual"] += stint.away_points
                a["off_expected"] += exp_away
                a["opp_actual"] += stint.home_points
                a["opp_expected"] += exp_home
                a["expected_net_minutes"] += (away_rate - home_rate) * stint.minutes
                a["team"] = away

            lineup_rows.append({
                "Game Sequence": game_sequence,
                "Game ID": gid,
                "Date": date,
                "Home": home,
                "Away": away,
                "Stint": stint_number,
                "Start Sec": round(stint.start, 3),
                "End Sec": round(stint.end, 3),
                "Minutes": round(stint.minutes, 6),
                "Home Points": stint.home_points,
                "Away Points": stint.away_points,
                "Expected Home Rate / 40": home_rate,
                "Expected Away Rate / 40": away_rate,
                "Expected Home Points": exp_home,
                "Expected Away Points": exp_away,
                "Home Offensive Residual": home_off_resid,
                "Home Defensive Residual": home_def_resid,
                "Home Net Residual": home_off_resid + home_def_resid,
                "Away Offensive Residual": away_off_resid,
                "Away Defensive Residual": away_def_resid,
                "Away Net Residual": away_off_resid + away_def_resid,
                **{f"Home {i+1}": all_names.get(pid, pid) for i, pid in enumerate(stint.home_ids)},
                **{f"Away {i+1}": all_names.get(pid, pid) for i, pid in enumerate(stint.away_ids)},
            })

        # Freeze all game expectations first, then update the chronological model.
        for stint in stints:
            for home_scoring, points in [(True, stint.home_points), (False, stint.away_points)]:
                idx, val = design_indices(stint, pidx, home_scoring)
                y = points / stint.minutes * 40.0 - NEUTRAL_RATE
                rls_update(beta, cov, idx, val, y, stint.minutes)

        # Player game rows and chronological rating history.
        for pid, agg in player_agg.items():
            mins = agg["minutes"]
            if mins <= 0:
                continue
            off_adj = agg["off_actual"] - agg["off_expected"]
            def_adj = agg["opp_expected"] - agg["opp_actual"]
            net_adj = off_adj + def_adj
            expected_net_rate = agg["expected_net_minutes"] / mins
            actual_net = (agg["off_actual"] - agg["opp_actual"])
            actual_net_rate = actual_net / mins * 40.0
            off_idx = 2 + pidx[pid]
            def_idx = 2 + len(pidx) + pidx[pid]
            pre_off = pre_beta[off_idx]
            pre_def = pre_beta[def_idx]
            post_off = beta[off_idx]
            post_def = beta[def_idx]
            cumulative_minutes[pid] += mins
            games_to_date[pid] += 1
            reliability = cumulative_minutes[pid] / (cumulative_minutes[pid] + PLAYER_PRIOR_MINUTES)
            player_game_rows.append({
                "Game Sequence": game_sequence,
                "Game ID": gid,
                "Date": date,
                "Player": all_names.get(pid, pid),
                "Player ID": pid,
                "Team": agg["team"],
                "Opponent": away if agg["team"] == home else home,
                "Minutes": mins,
                "Pregame Player Offence / 40": pre_off,
                "Pregame Player Defence / 40": pre_def,
                "Pregame Player Net / 40": pre_off + pre_def,
                "Expected Team Points While On": agg["off_expected"],
                "Actual Team Points While On": agg["off_actual"],
                "Offensive Adjusted Points": off_adj,
                "Expected Opponent Points While On": agg["opp_expected"],
                "Actual Opponent Points While On": agg["opp_actual"],
                "Defensive Adjusted Points": def_adj,
                "Net Adjusted Points": net_adj,
                "Net Adjusted Points / 40": net_adj / mins * 40.0,
                "Expected Lineup Net / 40": expected_net_rate,
                "Actual Lineup Net / 40": actual_net_rate,
                "Postgame Player Offence / 40": post_off,
                "Postgame Player Defence / 40": post_def,
                "Postgame Player Net / 40": post_off + post_def,
                "Postgame Rating Change": (post_off + post_def) - (pre_off + pre_def),
                "Games To Date": games_to_date[pid],
                "Minutes To Date": cumulative_minutes[pid],
                "Reliability": reliability,
            })
            player_history_rows.append({
                "Game Sequence": game_sequence,
                "Game ID": gid,
                "Date": date,
                "Player": all_names.get(pid, pid),
                "Player ID": pid,
                "Team": agg["team"],
                "Game Minutes": mins,
                "Games To Date": games_to_date[pid],
                "Minutes To Date": cumulative_minutes[pid],
                "Pregame Offence": pre_off,
                "Postgame Offence": post_off,
                "Offence Change": post_off - pre_off,
                "Pregame Defence": pre_def,
                "Postgame Defence": post_def,
                "Defence Change": post_def - pre_def,
                "Pregame Net": pre_off + pre_def,
                "Postgame Net": post_off + post_def,
                "Net Change": (post_off + post_def) - (pre_off + pre_def),
                "Reliability": reliability,
            })

        game_rows.append({
            "Game Sequence": game_sequence,
            "Game ID": gid,
            "Date": date,
            "Away": away,
            "Home": home,
            "Actual Away": game_actual_away,
            "Actual Home": game_actual_home,
            "Expected Away": game_expected_away,
            "Expected Home": game_expected_home,
            "Away Residual": game_actual_away - game_expected_away,
            "Home Residual": game_actual_home - game_expected_home,
            "Actual Home Margin": game_actual_home - game_actual_away,
            "Expected Home Margin": game_expected_home - game_expected_away,
            "Margin vs Expectation": (game_actual_home - game_actual_away) - (game_expected_home - game_expected_away),
            "Pregame Baseline / 40": NEUTRAL_RATE + pre_beta[0],
            "Pregame Home Court / 40": pre_beta[1],
            "Postgame Baseline / 40": NEUTRAL_RATE + beta[0],
            "Postgame Home Court / 40": beta[1],
        })

    # Ranks are raw, not blended or rescaled.
    ranked_total = sorted(player_game_rows, key=lambda r: r["Net Adjusted Points"], reverse=True)
    for rank, row in enumerate(ranked_total, 1):
        row["Total Impact Rank"] = rank
    rate_qualified = sorted([r for r in player_game_rows if r["Minutes"] >= 10], key=lambda r: r["Net Adjusted Points / 40"], reverse=True)
    rate_rank = {(r["Game ID"], r["Player ID"]): i for i, r in enumerate(rate_qualified, 1)}
    for row in player_game_rows:
        row["Rate Rank (10+ Min)"] = rate_rank.get((row["Game ID"], row["Player ID"]), "")
    player_game_rows.sort(key=lambda r: r["Total Impact Rank"])

    final_players = []
    for pid in player_ids:
        off = beta[2 + pidx[pid]]
        deff = beta[2 + len(pidx) + pidx[pid]]
        mins = cumulative_minutes[pid]
        final_players.append({
            "Player": all_names.get(pid, pid),
            "Player ID": pid,
            "Games": games_to_date[pid],
            "Minutes": mins,
            "Offence / 40": off,
            "Defence / 40": deff,
            "Net / 40": off + deff,
            "Reliability": mins / (mins + PLAYER_PRIOR_MINUTES) if mins else 0.0,
        })
    final_players.sort(key=lambda r: r["Net / 40"], reverse=True)
    for rank, row in enumerate(final_players, 1):
        row["Net Rank"] = rank

    player_game_fields = [
        "Total Impact Rank", "Rate Rank (10+ Min)", "Game Sequence", "Game ID", "Date", "Player", "Player ID", "Team", "Opponent", "Minutes",
        "Pregame Player Offence / 40", "Pregame Player Defence / 40", "Pregame Player Net / 40",
        "Expected Team Points While On", "Actual Team Points While On", "Offensive Adjusted Points",
        "Expected Opponent Points While On", "Actual Opponent Points While On", "Defensive Adjusted Points",
        "Net Adjusted Points", "Net Adjusted Points / 40", "Expected Lineup Net / 40", "Actual Lineup Net / 40",
        "Postgame Player Offence / 40", "Postgame Player Defence / 40", "Postgame Player Net / 40", "Postgame Rating Change",
        "Games To Date", "Minutes To Date", "Reliability",
    ]
    lineup_fields = list(lineup_rows[0].keys()) if lineup_rows else []
    history_fields = list(player_history_rows[0].keys()) if player_history_rows else []
    game_fields = list(game_rows[0].keys()) if game_rows else []
    final_fields = ["Net Rank", "Player", "Player ID", "Games", "Minutes", "Offence / 40", "Defence / 40", "Net / 40", "Reliability"]

    write_csv(out / "player_game_impact.csv", player_game_rows, player_game_fields)
    write_csv(out / "lineup_expectations.csv", lineup_rows, lineup_fields)
    write_csv(out / "player_history.csv", player_history_rows, history_fields)
    write_csv(out / "game_expectations.csv", game_rows, game_fields)
    write_csv(out / "final_player_ratings.csv", final_players, final_fields)
    write_csv(out / "game_reconstruction_audit.csv", [
        {
            "Game ID": a["game_id"], "Date": a["date"], "Away": a["away"], "Home": a["home"], "Stints": a["stints"],
            "Stint Away Points": a["stint_away_points"], "Final Away": a["final_away"], "Stint Home Points": a["stint_home_points"], "Final Home": a["final_home"],
            "Total Stint Minutes": a["minutes"], "Errors": len(a["errors"]), "Repairs": len(a["repairs"]),
        }
        for a in audits
    ], ["Game ID", "Date", "Away", "Home", "Stints", "Stint Away Points", "Final Away", "Stint Home Points", "Final Home", "Total Stint Minutes", "Errors", "Repairs"])

    cutoff_stints = sum(a["stints"] for a in audits if a["date"] <= "2026-07-09")
    cutoff_games = sum(1 for a in audits if a["date"] <= "2026-07-09")
    audit_summary = {
        "games": len(games),
        "first_date": audits[0]["date"] if audits else None,
        "last_date": audits[-1]["date"] if audits else None,
        "players": len(player_ids),
        "stints": len(lineup_rows),
        "player_games": len(player_game_rows),
        "missing_ids": missing,
        "games_through_2026_07_09": cutoff_games,
        "stints_through_2026_07_09": cutoff_stints,
        "target_old_games": 165,
        "target_old_stints": 5308,
        "score_reconciled_games": sum(1 for a in audits if abs(a["stint_home_points"] - a["final_home"]) < 1e-6 and abs(a["stint_away_points"] - a["final_away"]) < 1e-6),
        "games_with_errors": sum(1 for a in audits if a["errors"]),
        "total_errors": sum(len(a["errors"]) for a in audits),
        "total_repairs": sum(len(a["repairs"]) for a in audits),
        "final_baseline_per_40": NEUTRAL_RATE + beta[0],
        "final_home_court_per_40": beta[1],
        "method": "Strict chronological lineup residual model. No box score, result bonus, clutch bonus, opponent Elo blend, composite weights or 0-100 rescaling.",
        "limitation": "Players sharing the same stint share its lineup residual. Individual separation comes from substitution patterns across stints and the chronological regularized model; a single stint cannot identify one player causally.",
    }
    (out / "audit.json").write_text(json.dumps(audit_summary, indent=2), encoding="utf-8")
    (out / "methodology.txt").write_text(
        "\n".join([
            "PLAYER GAME IMPACT DEFINITION",
            "Offensive adjusted points = actual team points while the player was on court minus the points expected from the exact ten-player lineup before the game.",
            "Defensive adjusted points = expected opponent points while the player was on court minus actual opponent points.",
            "Net adjusted points = offensive adjusted points + defensive adjusted points.",
            "All lineup expectations are frozen before the game and use only earlier games.",
            "No box-score production, score-margin bonus, win bonus, clutch bonus, Elo component, hand-selected blend weights or 0-100 rescaling is used.",
            "The only exposure scaling is actual stint duration. Net adjusted points is the primary cumulative game measure; Net adjusted points per 40 is the rate measure.",
            "Limitation: teammates sharing a stint receive the same stint residual. The method controls for lineup strength chronologically but does not prove isolated causality within one shared stint.",
        ]), encoding="utf-8")
    print(json.dumps(audit_summary, indent=2))
    print("TOP 15 TOTAL IMPACT")
    for r in player_game_rows[:15]:
        print(r["Total Impact Rank"], r["Date"], r["Player"], r["Team"], r["Opponent"], round(r["Net Adjusted Points"], 3), round(r["Net Adjusted Points / 40"], 3), round(r["Minutes"], 2))


if __name__ == "__main__":
    main()
