import json, glob, os
from pathlib import Path
from urllib.request import urlopen

OUT=Path('inspect_output.txt')
lines=[]
for gid in [401857054,401857055,401857091]:
    url=f'https://raw.githubusercontent.com/sportsdataverse/wehoop-wnba-raw/main/wnba/json/final/{gid}.json'
    data=json.load(urlopen(url))
    lines.append(f'GAME {gid}')
    lines.append(f'top keys={sorted(data.keys())}')
    for tb in (data.get('boxscore') or {}).get('players') or []:
        team=(tb.get('team') or {}).get('abbreviation')
        lines.append(f'TEAM {team}')
        for group in tb.get('statistics') or []:
            for ae in group.get('athletes') or []:
                ath=ae.get('athlete') or {}
                if ae.get('starter') or ae.get('didNotPlay') is False:
                    lines.append('ATH '+json.dumps({k:ae.get(k) for k in ae.keys() if k not in ['stats','athlete']},sort_keys=True)+' '+str(ath.get('displayName')))
            break
    subs=[]
    for p in data.get('plays') or []:
        typ=str(p.get('type.text') or '')
        txt=str(p.get('text') or '')
        if 'sub' in typ.lower() or 'enters' in txt.lower():
            subs.append({k:p.get(k) for k in p.keys() if k in ['game_play_number','type.id','type.text','text','team.id','participants.0.athlete.id','participants.1.athlete.id','participants.2.athlete.id','period.number','clock.displayValue','start.game_seconds_remaining']})
    lines.append(f'SUB COUNT {len(subs)}')
    for x in subs[:12]: lines.append(json.dumps(x,sort_keys=True))
OUT.write_text('\n'.join(lines),encoding='utf-8')
print('\n'.join(lines[:80]))
