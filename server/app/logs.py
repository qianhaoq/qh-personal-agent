"""第三方库日志级别钉扎（INV-14 框架层，threads9）。

httpx 的 INFO 级会打每个请求行，未来版本可能打出请求细节（LLM 请求体
含笔记正文这类用户内容）；httpcore 是 httpx 的传输层（DEBUG 级打帧）；
openai SDK 本仓当前未用，logger 名一并预留——库升级或换入时不需要
再想起这一层。不设置的话级别随部署环境默认漂移，等库把用户内容打进
日志时已无防线。

`create_app`（api 进程）与 worker `main()`（basicConfig 之后）统一调用，
两个入口都不许漏。
"""
import logging

_PINNED_LOGGERS = ("httpx", "httpcore", "openai")


def harden_third_party_loggers() -> None:
    """把第三方 HTTP 客户端库的 logger 钉到 >= WARNING（只升不降：
    环境本来配得更严格（如 ERROR）时不动它——effective level 已达标的
    logger 不会被改）。"""
    for name in _PINNED_LOGGERS:
        lg = logging.getLogger(name)
        if lg.getEffectiveLevel() < logging.WARNING:
            lg.setLevel(logging.WARNING)
