"""Small shared helpers (ids, clocks, JSON)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


def new_id() -> str:
    return str(uuid.uuid4())


def now_dt() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return iso(now_dt())


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def ms_between(start_iso: str | None, end: datetime | None = None) -> int:
    start = parse_iso(start_iso)
    if start is None:
        return 0
    end = end or now_dt()
    return max(0, int((end - start).total_seconds() * 1000))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def local_timezone_name() -> str:
    """Best-effort timezone label. IANA names are preferred when the environment provides one."""
    import os

    tz = os.environ.get("TZ")
    if tz:
        return tz
    offset = now_dt().utcoffset()
    if offset is None:
        return "UTC"
    minutes = int(offset.total_seconds() // 60)
    if minutes == 540:
        return "Asia/Tokyo"
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"
