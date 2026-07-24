from __future__ import annotations

import json
from pathlib import Path

from run import candidates, download, events, panel

OUT = Path('research/phase35_crossvenue_liquidation/results')
OUT.mkdir(parents=True, exist_ok=True)

# Use the latest ten complete dates. The inventory records all currently available
# complete dates separately. Ten days are discovery-only; promotion remains forbidden.
dates, paths = download(days=10)
frame = panel(paths)
frame.to_parquet(OUT / 'PANEL_1S.parquet', index=False)
event_frame = events(frame)
event_frame.to_csv(OUT / 'EVENTS.csv', index=False)
books = {venue: group.set_index('sec').sort_index() for venue, group in frame.groupby('exchange')}
candidate_frame = candidates(event_frame, books)
candidate_frame.to_csv(OUT / 'CANDIDATES.csv', index=False)
summary = {
    'dates': dates,
    'date_count': len(dates),
    'rows_1s': len(frame),
    'events': len(event_frame),
    'candidate_rows': len(candidate_frame),
    'policies': int(candidate_frame.policy_id.nunique()) if len(candidate_frame) else 0,
    'timestamp': 'exchange timestamp only; historical discovery grade',
    'causality': 'current liquidation burst, fixed confirmation delay, no future cluster maximum',
}
(OUT / 'GENERATION.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
print(json.dumps(summary, indent=2, sort_keys=True))
