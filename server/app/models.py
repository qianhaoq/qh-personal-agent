"""数据库模型（S1 子集：users/devices/invite_codes/notes/reminders/jobs/sequences）。"""
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Sequence(Base):
    """全局单调序列，作为 /v1/sync 的 cursor 来源。"""

    __tablename__ = "sequences"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    status: Mapped[str] = mapped_column(String(16), default="active")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used: Mapped[int] = mapped_column(Integer, default=0)
    # 该邀请码创建的账号：同一码后续登录复用同一 user（used 只计注册次数）
    owner_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(32), default="android")
    refresh_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 上一枚 refresh 的哈希：仅用于检测轮换后的复用（复用即吊销设备）
    prev_refresh_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (UniqueConstraint("user_id", "client_note_id", name="uq_note_user_client"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_note_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="app")
    asr_meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    # uploaded -> organized | failed（整理期间不再落库 organizing 中间态：
    # LLM 调用在事务外，没有可以承载它的短事务）
    status: Mapped[str] = mapped_column(String(16), default="uploaded", index=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    todos: Mapped[list | None] = mapped_column(JSON, nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    # 整理产物已写入 MemoryStore。记忆写入在 DB 事务提交之后（mem0 内部
    # 还有 LLM/embedding 调用），失败靠 job 重试补写（见 pipeline/organize）
    memory_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rev: Mapped[int] = mapped_column(Integer, default=1)
    change_seq: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        Index("ix_reminders_user_note", "user_id", "note_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[str | None] = mapped_column(ForeignKey("notes.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rrule: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # suggested | scheduled | done | cancelled
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)
    rev: Mapped[int] = mapped_column(Integer, default=1)
    change_seq: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 持有租约的 worker 实例 id：收尾按 (status, locked_by, locked_at) 校验，
    # 任务被回收换主后旧 worker 的收尾不再生效（见 jobs.queue.finalize_claim）
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


# 兼容占位：mem0 后端自身的表由 mem0 管理，不在这里定义。
