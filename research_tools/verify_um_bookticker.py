#!/usr/bin/env python3
"""Verify public Binance Vision USD-M bookTicker archive availability and schema."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

SYMBOL = "BTCUSDT"
DAY = "2023-06-27"
MONTH = "2023-06"
ROOT = "https://data.binance.vision/data/futures/um"
CANDIDATES = (
    (
        "daily",
        f"{ROOT}/daily/bookTicker/{SYMBOL}/{SYMBOL}-bookTicker-{DAY}.zip",
    ),
    (
        "monthly",
        f"{ROOT}/monthly/bookTicker/{SYMBOL}/{SYMBOL}-bookTicker-{MONTH}.zip",
    ),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, path: Path) -> tuple[bool, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": "smc-ict-bookticker-audit/1.1"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response, path.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        return True, None
    except urllib.error.HTTPError as exc:
        path.unlink(missing_ok=True)
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # preserve an audit record rather than guessing availability
        path.unlink(missing_ok=True)
        return False, f"{type(exc).__name__}: {exc}"


def checksum_value(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    candidates = [
        token.lower()
        for token in text.replace("*", " ").split()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token)
    ]
    if not candidates:
        raise ValueError(f"no SHA-256 in {path}")
    return candidates[0]


def inspect_archive(archive: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive) as bundle:
        names = [name for name in bundle.namelist() if not name.endswith("/")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV member: {names}")
        with bundle.open(names[0]) as raw:
            reader = csv.reader(line.decode("utf-8-sig") for line in raw)
            samples: list[list[str]] = []
            field_counts: dict[int, int] = {}
            rows_seen = 0
            for row in reader:
                if not row:
                    continue
                rows_seen += 1
                field_counts[len(row)] = field_counts.get(len(row), 0) + 1
                if len(samples) < 8:
                    samples.append(row)
                if rows_seen >= 100_001:
                    break
    return {
        "zip_member": names[0],
        "sample_rows": samples,
        "field_counts_first_100001_nonempty_rows": field_counts,
    }


def main() -> int:
    out = Path("research_artifact/bookticker_schema")
    out.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, object]] = []
    selected: dict[str, object] | None = None
    for granularity, url in CANDIDATES:
        filename = url.rsplit("/", 1)[-1]
        archive = out / filename
        checksum = out / f"{filename}.CHECKSUM"
        checksum_ok, checksum_error = download(url + ".CHECKSUM", checksum)
        archive_ok, archive_error = download(url, archive)
        attempt = {
            "granularity": granularity,
            "url": url,
            "checksum_available": checksum_ok,
            "archive_available": archive_ok,
            "checksum_error": checksum_error,
            "archive_error": archive_error,
        }
        if checksum_ok != archive_ok:
            attempt["availability_mismatch"] = True
        if checksum_ok and archive_ok:
            published = checksum_value(checksum)
            observed = sha256(archive)
            if published != observed:
                raise ValueError(f"checksum mismatch for {filename}: {published} != {observed}")
            selected = {
                **attempt,
                "published_sha256": published,
                "observed_sha256": observed,
                "archive_bytes": archive.stat().st_size,
                **inspect_archive(archive),
            }
        attempts.append(attempt)
        archive.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        if selected is not None:
            break

    manifest = {
        "status": "RESEARCH_ONLY_SCHEMA_AUDIT",
        "attempts": attempts,
        "selected": selected,
        "credentials_used": False,
        "orders_submitted": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)
    if selected is None:
        raise SystemExit("no daily or monthly USD-M bookTicker archive was available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
