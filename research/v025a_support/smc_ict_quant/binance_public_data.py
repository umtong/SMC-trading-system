from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

BASE_URL = "https://data.binance.vision"
OFFICIAL_BASE_URLS = (
    BASE_URL,
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision",
)
KLINE_DATASETS = {
    "klines",
    "markPriceKlines",
    "indexPriceKlines",
    "premiumIndexKlines",
}
SUPPORTED_DATASETS = KLINE_DATASETS | {
    "aggTrades",
    "trades",
    "fundingRate",
    "bookTicker",
    "metrics",
    "bookDepth",
}

DATASET_CADENCES: dict[str, frozenset[str]] = {
    "klines": frozenset({"daily", "monthly"}),
    "aggTrades": frozenset({"daily", "monthly"}),
    "trades": frozenset({"daily", "monthly"}),
    "fundingRate": frozenset({"monthly"}),
    "bookTicker": frozenset({"daily", "monthly"}),
    "bookDepth": frozenset({"daily"}),
    "indexPriceKlines": frozenset({"daily", "monthly"}),
    "markPriceKlines": frozenset({"daily", "monthly"}),
    "premiumIndexKlines": frozenset({"daily", "monthly"}),
    "metrics": frozenset({"daily"}),
}


@dataclass(frozen=True, slots=True)
class ArchivePartition:
    cadence: Literal["monthly", "daily"]
    label: str


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    symbol: str
    dataset: str
    partition: ArchivePartition
    interval: str | None = None
    market_path: str = "futures/um"

    def __post_init__(self) -> None:
        if self.dataset not in SUPPORTED_DATASETS:
            raise ValueError(f"unsupported dataset: {self.dataset}")
        allowed_cadences = DATASET_CADENCES[self.dataset]
        if self.partition.cadence not in allowed_cadences:
            allowed = ", ".join(sorted(allowed_cadences))
            raise ValueError(
                f"{self.dataset} is unavailable at {self.partition.cadence} cadence; "
                f"allowed: {allowed}"
            )
        requires_interval = self.dataset in KLINE_DATASETS
        if requires_interval and not self.interval:
            raise ValueError(f"{self.dataset} requires an interval")
        if not requires_interval and self.interval is not None:
            raise ValueError(f"{self.dataset} does not use an interval")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase text")

    @property
    def filename(self) -> str:
        if self.dataset in KLINE_DATASETS:
            return f"{self.symbol}-{self.interval}-{self.partition.label}.zip"
        return f"{self.symbol}-{self.dataset}-{self.partition.label}.zip"

    @property
    def relative_path(self) -> str:
        pieces = [
            "data",
            self.market_path,
            self.partition.cadence,
            self.dataset,
            self.symbol,
        ]
        if self.dataset in KLINE_DATASETS:
            pieces.append(str(self.interval))
        pieces.append(self.filename)
        return "/".join(pieces)

    def url_for(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/{self.relative_path}"

    @property
    def url(self) -> str:
        return self.url_for(BASE_URL)

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"


def _month_end(day: date) -> date:
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def iter_partitions(start: date, end: date) -> Iterable[ArchivePartition]:
    """Use monthly archives for complete months and daily archives at edges.

    Both ``start`` and ``end`` are inclusive.  This keeps partial-month requests
    precise while minimizing archive count for long research histories.
    """

    if end < start:
        raise ValueError("end date precedes start date")
    cursor = start
    while cursor <= end:
        first = cursor.replace(day=1)
        last = _month_end(cursor)
        if cursor == first and last <= end:
            yield ArchivePartition("monthly", cursor.strftime("%Y-%m"))
            cursor = last + timedelta(days=1)
        else:
            yield ArchivePartition("daily", cursor.isoformat())
            cursor += timedelta(days=1)


def iter_dataset_partitions(dataset: str, start: date, end: date) -> Iterable[ArchivePartition]:
    """Yield only archive partitions that exist for a specific dataset.

    Binance Vision publishes ``metrics`` daily only and ``fundingRate``
    monthly only.  Funding months intersecting a partial requested range are
    still downloaded and must be clipped after normalization.
    """

    if dataset not in SUPPORTED_DATASETS:
        raise ValueError(f"unsupported dataset: {dataset}")
    if end < start:
        raise ValueError("end date precedes start date")
    cadences = DATASET_CADENCES[dataset]
    if cadences == frozenset({"daily"}):
        cursor = start
        while cursor <= end:
            yield ArchivePartition("daily", cursor.isoformat())
            cursor += timedelta(days=1)
        return
    if cadences == frozenset({"monthly"}):
        cursor = start.replace(day=1)
        final = end.replace(day=1)
        while cursor <= final:
            yield ArchivePartition("monthly", cursor.strftime("%Y-%m"))
            cursor = (_month_end(cursor) + timedelta(days=1)).replace(day=1)
        return
    yield from iter_partitions(start, end)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(text: str, expected_filename: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty checksum response")
    fields = lines[0].replace("*", " ").split()
    if len(fields) < 2:
        raise ValueError(f"malformed checksum line: {lines[0]!r}")
    digest, filename = fields[0].lower(), fields[-1]
    if filename != expected_filename:
        raise ValueError(f"checksum filename mismatch: {filename!r} != {expected_filename!r}")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("checksum is not SHA-256")
    return digest


def _fetch_bytes(url: str, timeout: float, retries: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "smc-ict-quant/0.2"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            if attempt >= retries or exc.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= retries:
                raise
        time.sleep(min(2**attempt, 30))
    raise RuntimeError("unreachable retry state")


def _fetch_from_official_bases(
    request: ArchiveRequest,
    *,
    checksum: bool,
    base_urls: Iterable[str],
    timeout: float,
    retries: int,
) -> tuple[bytes, str]:
    failures: list[str] = []
    for base_url in tuple(base_urls):
        url = request.url_for(base_url) + (".CHECKSUM" if checksum else "")
        try:
            return _fetch_bytes(url, timeout, retries), url
        except FileNotFoundError:
            failures.append(f"404 {url}")
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {url}: {exc}")
    kind = "checksum" if checksum else "archive"
    detail = "; ".join(failures)
    if failures and all(item.startswith("404 ") for item in failures):
        raise FileNotFoundError(f"{kind} unavailable on official endpoints: {detail}")
    raise ConnectionError(f"failed to fetch {kind} from official endpoints: {detail}")


def _safe_extract(archive: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe zip member: {member.filename}")
            zipped.extract(member, destination)
            if not member.is_dir():
                extracted.append(target)
    return extracted


def download_archive(
    request: ArchiveRequest,
    output_root: Path,
    *,
    timeout: float = 120.0,
    retries: int = 4,
    extract: bool = False,
    overwrite: bool = False,
    base_urls: Iterable[str] = OFFICIAL_BASE_URLS,
) -> dict[str, object]:
    """Download one official archive, verify its checksum, and record provenance."""

    archive_path = output_root / request.relative_path
    checksum_path = archive_path.with_name(f"{archive_path.name}.CHECKSUM")
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    checksum_bytes, checksum_url = _fetch_from_official_bases(
        request,
        checksum=True,
        base_urls=base_urls,
        timeout=timeout,
        retries=retries,
    )
    checksum_text = checksum_bytes.decode("utf-8")
    expected_digest = parse_checksum(checksum_text, request.filename)
    checksum_path.write_bytes(checksum_bytes)
    archive_url = request.url

    if overwrite or not archive_path.exists() or sha256_file(archive_path) != expected_digest:
        payload, archive_url = _fetch_from_official_bases(
            request,
            checksum=False,
            base_urls=base_urls,
            timeout=timeout,
            retries=retries,
        )
        temporary = archive_path.with_suffix(f"{archive_path.suffix}.part")
        temporary.write_bytes(payload)
        actual = sha256_file(temporary)
        if actual != expected_digest:
            temporary.unlink(missing_ok=True)
            raise ValueError(
                f"checksum mismatch for {request.filename}: {actual} != {expected_digest}"
            )
        temporary.replace(archive_path)

    actual_digest = sha256_file(archive_path)
    if actual_digest != expected_digest:
        raise ValueError(f"existing archive checksum mismatch: {archive_path}")

    extracted: list[str] = []
    if extract:
        extract_dir = archive_path.parent / "extracted"
        extracted = [str(path) for path in _safe_extract(archive_path, extract_dir)]

    return {
        "request": asdict(request),
        "url": archive_url,
        "checksum_url": checksum_url,
        "official_base_urls_attempted": list(base_urls),
        "archive_path": str(archive_path),
        "sha256": actual_digest,
        "bytes": archive_path.stat().st_size,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "extracted_files": extracted,
    }


def append_manifest(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
