"""时间工具：统一 UTC。

SQLite 方言存 datetime 时不保留时区偏移（值本身是 UTC 墙钟），读回来是 naive；
PostgreSQL timestamptz 读回来是 aware。as_utc 把两种情况都归一成 aware UTC。
"""
from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt is not None else None
