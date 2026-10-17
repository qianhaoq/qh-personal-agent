"""整理 worker 入口：python -m app.jobs.worker。"""
import asyncio
import logging

from app.config import get_settings
from app.db import startup_create_all
from app.pipeline.runner import run_pending_jobs

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


async def main() -> None:
    settings = get_settings()
    # PG 下不建表（先跑 init-db，见 INV-9）；SQLite 本地开发保留
    await startup_create_all()
    logger.info("worker started (interval=%.1fs)", settings.worker_poll_interval_seconds)
    while True:
        try:
            n = await run_pending_jobs(limit=settings.worker_batch_size)
            if n == 0:
                await asyncio.sleep(settings.worker_poll_interval_seconds)
        except Exception:  # 常驻进程不能退出：任何异常只记日志
            logger.exception("worker loop error")
            await asyncio.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
