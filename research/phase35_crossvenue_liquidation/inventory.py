from __future__ import annotations

import json
import re
from pathlib import Path

from huggingface_hub import list_repo_files

REPO='azulcoder/btc-quant-ticks'
TABLES=('trades','liquidations','depth_snapshots')
OUT=Path('research/phase35_crossvenue_liquidation/results')
OUT.mkdir(parents=True,exist_ok=True)

files=list_repo_files(REPO,repo_type='dataset')
pattern=re.compile(r'^data/date=(\d{4}-\d{2}-\d{2})/(trades|liquidations|depth_snapshots)\.parquet$')
by={}
for path in files:
    match=pattern.match(path)
    if match:
        date,table=match.groups();by.setdefault(date,set()).add(table)
complete=sorted(date for date,tables in by.items() if set(TABLES)<=tables)
payload={'repo':REPO,'complete_date_count':len(complete),'first_complete_date':complete[0] if complete else None,'last_complete_date':complete[-1] if complete else None,'complete_dates':complete}
(OUT/'DATASET_INVENTORY.json').write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
print(json.dumps({k:payload[k] for k in ('complete_date_count','first_complete_date','last_complete_date')},indent=2))
