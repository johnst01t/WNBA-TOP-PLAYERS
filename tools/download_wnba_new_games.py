#!/usr/bin/env python3
from __future__ import annotations
import argparse, csv, json, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-raw/main/wnba/json/final/{game_id}.json"
UA = "WNBA-update/2026"

def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict): out.update(flatten(v, key))
            elif isinstance(v, list): out[key] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            else: out[key] = v
    return out

def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields=[]; seen=set()
    for row in rows:
        for k in row:
            if k not in seen: seen.add(k); fields.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w=csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--start-id",type=int,default=401857054); p.add_argument("--end-id",type=int,default=401857091); p.add_argument("--output-dir",default="WNBA_2026_New_Games")
    a=p.parse_args(); out=Path(a.output_dir); raw=out/"raw_json"; raw.mkdir(parents=True,exist_ok=True)
    manifests=[]; plays=[]; player_boxes=[]; team_boxes=[]
    for gid in range(a.start_id,a.end_id+1):
        req=urllib.request.Request(BASE_URL.format(game_id=gid),headers={"User-Agent":UA})
        try:
            with urllib.request.urlopen(req,timeout=60) as r: payload=r.read()
        except urllib.error.HTTPError as e:
            if e.code==404: print("missing",gid); continue
            raise
        data=json.loads(payload.decode("utf-8")); (raw/f"{gid}.json").write_bytes(payload)
        gp=data.get("plays") or []; first=gp[0] if gp else {}; last=gp[-1] if gp else {}
        manifests.append({"game_id":gid,"date_time":first.get("wallclock"),"away_team":first.get("awayTeamAbbrev"),"home_team":first.get("homeTeamAbbrev"),"away_final_score":last.get("awayScore"),"home_final_score":last.get("homeScore"),"play_rows":len(gp),"source_url":BASE_URL.format(game_id=gid)})
        for play in gp:
            row={"source_game_id":gid}; row.update(flatten(play)); plays.append(row)
        box=data.get("boxscore") or {}
        for block in box.get("teams") or []:
            row={"source_game_id":gid}; team=block.get("team") or {}; row.update({f"team.{k}":v for k,v in flatten(team).items()})
            for i,stat in enumerate(block.get("statistics") or []):
                name=stat.get("name") or stat.get("abbreviation") or stat.get("label") or f"stat_{i}"; row[f"stat.{name}"]=stat.get("displayValue",stat.get("value"))
            team_boxes.append(row)
        for team_block in box.get("players") or []:
            team=team_block.get("team") or {}; teamflat=flatten(team)
            for gi,group in enumerate(team_block.get("statistics") or []):
                labels=group.get("labels") or group.get("names") or []
                for ae in group.get("athletes") or []:
                    row={"source_game_id":gid,"stat_group_index":gi,"stat_group_name":group.get("name") or group.get("displayName") or f"group_{gi}","starter":ae.get("starter"),"did_not_play":ae.get("didNotPlay"),"active":ae.get("active"),"ejected":ae.get("ejected"),"reason":ae.get("reason")}
                    row.update({f"team.{k}":v for k,v in teamflat.items()}); athlete=ae.get("athlete") or {}; row.update({f"athlete.{k}":v for k,v in flatten(athlete).items()})
                    for si,val in enumerate(ae.get("stats") or []): row[f"stat.{labels[si] if si < len(labels) else f'stat_{si}'}"]=val
                    player_boxes.append(row)
        print(gid,len(gp)); time.sleep(.05)
    write_csv(out/"WNBA_2026_New_Games_Manifest.csv",manifests); write_csv(out/"WNBA_2026_New_Games_Play_by_Play.csv",plays); write_csv(out/"WNBA_2026_New_Games_Player_Box_Scores.csv",player_boxes); write_csv(out/"WNBA_2026_New_Games_Team_Box_Scores.csv",team_boxes)
    (out/"audit.json").write_text(json.dumps({"games":len(manifests),"plays":len(plays),"player_box_rows":len(player_boxes),"first_game":manifests[0] if manifests else None,"last_game":manifests[-1] if manifests else None},indent=2),encoding="utf-8")
if __name__=="__main__": main()
