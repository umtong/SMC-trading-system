from pathlib import Path
import sys

OLD = '''def align_funding(frame: pd.DataFrame, funding: pd.DataFrame) -> np.ndarray:\n    out = np.zeros(len(frame), dtype=np.float64)\n    if funding.empty:\n        return out\n    mapping = dict(zip(funding["funding_time"].astype("int64"), funding["funding_rate"].astype(float)))\n    times = frame["open_time"].astype("int64").to_numpy()\n    for i, value in enumerate(times):\n        out[i] = float(mapping.get(int(value), 0.0))\n    return out\n'''
NEW = '''def align_funding(frame: pd.DataFrame, funding: pd.DataFrame) -> np.ndarray:\n    out = np.zeros(len(frame), dtype=np.float64)\n    if funding.empty:\n        return out\n    # Binance Vision funding archives can encode settlement timestamps a few\n    # milliseconds after the nominal 00:00/08:00/16:00 boundary. Normalize only\n    # the timestamp key; never forward-fill the rate to non-settlement bars.\n    normalized = funding.copy()\n    normalized["funding_key"] = pd.to_datetime(normalized["funding_time"], utc=True).dt.floor("5min")\n    normalized = normalized.sort_values("funding_time").drop_duplicates("funding_key", keep="last")\n    mapping = dict(zip(normalized["funding_key"].astype("int64"), normalized["funding_rate"].astype(float)))\n    times = pd.to_datetime(frame["open_time"], utc=True).astype("int64").to_numpy()\n    for i, value in enumerate(times):\n        out[i] = float(mapping.get(int(value), 0.0))\n    return out\n'''

def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_causal_funding_alignment.py SCRIPT")
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError("funding alignment anchor missing")
    path.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")

if __name__ == "__main__":
    main()
