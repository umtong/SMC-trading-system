from __future__ import annotations

import sys
from pathlib import Path

root = Path(sys.argv[1])
script = root / "eth_qh_external.py"
test = root / "test_eth_qh_external.py"

source = script.read_text(encoding="utf-8")
old = "'number_of_trades':'trade_count', 'taker_buy_base_asset_volume':'taker_buy_volume',"
new = "'number_of_trades':'trade_count', 'count':'trade_count', 'taker_buy_base_asset_volume':'taker_buy_volume',"
if old not in source:
    raise RuntimeError("expected Binance header alias block not found")
source = source.replace(old, new, 1)
script.write_text(source, encoding="utf-8")

tests = test.read_text(encoding="utf-8")
tests += '''\n\ndef test_binance_2025_count_header_alias_is_registered():\n    source = P.read_text(encoding="utf-8")\n    assert "'count':'trade_count'" in source\n'''
test.write_text(tests, encoding="utf-8")
