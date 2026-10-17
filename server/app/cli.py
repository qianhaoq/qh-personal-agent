"""管理 CLI：python -m app.cli <command>。

用法：
    python -m app.cli create-invite [--uses N] [--expires-hours H]
"""
import argparse
import asyncio
import secrets

from app.db import create_all
from app.models import InviteCode
from app.timeutil import utcnow


async def create_invite(uses: int, expires_hours: float | None) -> None:
    from app.db import get_sessionmaker

    await create_all()
    code = secrets.token_hex(8)
    from datetime import timedelta

    expires_at = utcnow() + timedelta(hours=expires_hours) if expires_hours else None
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(InviteCode(code=code, max_uses=uses, expires_at=expires_at))
        await session.commit()
    print(f"invite code: {code} (max_uses={uses}, expires_at={expires_at})")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_invite = sub.add_parser("create-invite", help="创建邀请码")
    p_invite.add_argument("--uses", type=int, default=1, help="可注册账号数")
    p_invite.add_argument("--expires-hours", type=float, default=None, help="有效时长（小时）")

    args = parser.parse_args()
    if args.command == "create-invite":
        asyncio.run(create_invite(args.uses, args.expires_hours))


if __name__ == "__main__":
    main()
