"""整理 worker 入口：python -m app.jobs.worker。"""
import asyncio
import logging

from app.config import Settings, get_settings
from app.db import startup_create_all
from app.errors import safe_error_text
from app.pipeline.runner import run_pending_jobs

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


def check_memory_backend_shareable(settings: Settings) -> None:
    """INV-10.4（threads5 P2-3）：worker 是独立进程，MEMORY_BACKEND=inmemory
    时它写的记忆对 api 进程的 /search 永远不可见——**无论放行标志**都拒绝
    inmemory+PostgreSQL（ALLOW_INMEMORY_IN_TESTS 只为单进程跑 PG 测试套件
    的 pytest 放行，不是给独立 worker 的）。disabled（显式空后端，threads7
    拆分）不受限；生产记忆后端由后续 PR（feat/mem0-backend）提供。"""
    if settings.database_url.startswith("postgresql") and settings.memory_backend == "inmemory":
        raise SystemExit(
            "worker 进程不允许 MEMORY_BACKEND=inmemory + PostgreSQL："
            "进程内记忆对 api 进程不可见（worker 写的记忆 /search 搜不到）。"
            "生产记忆后端将由后续 PR（feat/mem0-backend）提供，过渡期可显式设 "
            "MEMORY_BACKEND=disabled；单进程跑 PG 测试套件请直接跑 pytest，"
            "不要另起 worker。"
        )


async def main() -> None:
    settings = get_settings()
    check_memory_backend_shareable(settings)
    # PG 下不建表（先跑 init-db，见 INV-9）；SQLite 本地开发保留
    await startup_create_all()
    logger.info("worker started (interval=%.1fs)", settings.worker_poll_interval_seconds)
    while True:
        try:
            n = await run_pending_jobs(limit=settings.worker_batch_size)
            if n == 0:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
        except Exception as exc:  # noqa: BLE001 常驻进程不能退出：任何异常只记日志
            # INV-14（threads8 P1）：不用 logger.exception/exc_info=True——
            # traceback 会带异常消息原文（可能内嵌笔记正文等用户内容），
            # 只记类型名+错误码
            logger.error("worker loop error: %s", safe_error_text(exc))
            await asyncio.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
