#!/usr/bin/env python3
"""Verify one public Binance Vision USD-M bookTicker archive without trading access."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

SYMBOL = "BTCUSDT"
DAY = "2023-06-27"
BASE = "https://data.binance.vision/data/futures/um/daily/bookTicker"
FILENAME = f"{SYMBOL}-bookTicker-{DAY}.zip"
URL = f"{BASE}/{SYMBOL}/{FILENAME}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, path: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "smc-ict-bookticker-audit/1.0"})
    with urllib.request.urlopen(request, timeout=240) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def main() -> int:
    out = Path("research_artifact/bookticker_schema")
    out.mkdir(parents=True, exist_ok=True)
    archive = out / FILENAME
    checksum = out / f"{FILENAME}.CHECKSUM"
    download(URL + ".CHECKSUM", checksum)
    download(URL, archive)
    checksum_text = checksum.read_text(encoding="utf-8").strip()
    published = next(
        token.lower()
        for token in checksum_text.replace("*", " ").split()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token)
    )
    observed = sha256(archive)
    if published != observed:
        raise ValueError(f"checksum mismatch: {published} != {observed}")

    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV member: {names}")
        with bundle.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            samples = []
            field_counts: dict[int, int] = {}
            for index, row in enumerate(reader):
                if not row:
                    continue
                field_counts[len(row)] = field_counts.get(len(row), 0) + 1
                if len(samples) < 8:
                    samples.append(row)
                if index >= 100_000:
                    break
    manifest = {
        "status": "RESEARCH_ONLY_SCHEMA_AUDIT",
        "url": URL,
        "published_sha256": published,
        "observed_sha256": observed,
        "archive_bytes": archive.stat().st_size,
        "zip_member": names[0],
        "sample_rows": samples,
        "field_counts_first_100001_nonempty_rows": field_counts,
        "credentials_used": False,
        "orders_submitted": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive.unlink()
    checksum.unlink()
    print(json.dumps(manifest, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
