"""时间工具：统一 UTC。

SQLite 方言存 datetime 时不保留时区偏移（值本身是 UTC 墙钟），读回来是 naive；
PostgreSQL timestamptz 读回来是 aware。as_utc 把两种情况都归一成 aware UTC。
"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_utc(dt: datetime | None, tzname: str) -> datetime | None:
    """按 tzname（IANA 名）把时间归一成 UTC：naive 视为该时区本地时间。

    客户端不带时区的时间（client_time / recorded_at）按请求时区本地化，
    而不是误当成 UTC。aware 直接转 UTC。未知时区名兜底按 UTC 解释
    （调用方一般已用 schema 校验过时区，这里只是防御）。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC
    return dt.replace(tzinfo=tz).astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    return as_utc(dt).isoformat() if dt is not None else None
