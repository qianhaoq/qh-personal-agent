FROM python:3.12-slim

WORKDIR /srv/app

# 运行时镜像只装生产依赖（requirements-app.txt）：不装 pytest 等测试工具。
# 记忆后端：生产实现（mem0 + pgvector）已拆分至 feat/mem0-backend 分支，
# 随该分支回到镜像依赖；过渡期 compose 显式设 MEMORY_BACKEND=disabled
COPY requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt

COPY app ./app

# 非 root 运行（P2 审查）：容器内进程无需写应用目录，只可能写运行时
# 临时目录；显式收敛属主，避免以 root 跑业务代码
RUN useradd --system --uid 10001 --no-create-home appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

ENV PYTHONUNBUFFERED=1

# api 默认入口；worker 用 compose 覆盖 command。
# --no-access-log（INV-14，threads9）：请求行含完整 query（用户内容），
# 与 docker-compose.yml 的 api command 保持一致
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
