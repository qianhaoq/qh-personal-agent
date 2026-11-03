"""Watchdog 入口：python -m app.jobs.watchdog。

Control Loop（控制循环）：只跑 `app.runtime.repair`——回收过期 Assignment、
补扫缺 Trigger 的 uploaded note。不认领整理 Ticket、不碰独立 memory 队列
（那是 app.jobs.worker 的职责）；与 Worker 可同时常驻，多进程扫同一 Store，
CAS 保证同一 Ticket 至多一个执行者。不写记忆，因此不做 worker 的
INV-10.4 检查。
"""
import asyncio
import logging

from app.config import get_settings
from app.db import startup_create_all
from app.errors import safe_error_text
from app.logs import harden_third_party_loggers
from app.runtime import repair

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# 第三方 HTTP 客户端库 logger 钉 >= WARNING（INV-14 框架层，threads9）：
# watchdog 不经 create_app，必须在 basicConfig 之后自己调
harden_third_party_loggers()
logger = logging.getLogger("watchdog")


async def main() -> None:
    settings = get_settings()
    # PG 下不建表（先跑 init-db，见 INV-9）；SQLite 本地开发保留
    await startup_create_all()
    logger.info("watchdog started (interval=%.1fs)", settings.worker_poll_interval_seconds)
    while True:
        try:
            if await repair(limit=settings.worker_batch_size) == 0:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
        except Exception as exc:  # noqa: BLE001 常驻进程不能退出：任何异常只记日志
            # INV-14（threads8 P1）：不用 logger.exception/exc_info=True——
            # traceback 会带异常消息原文（可能内嵌笔记正文等用户内容），
            # 只记类型名+错误码
            logger.error("watchdog loop error: %s", safe_error_text(exc))
            await asyncio.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
