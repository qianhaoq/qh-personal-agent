FROM python:3.12-slim

WORKDIR /srv/app

# 运行时镜像：业务依赖 + mem0 生产记忆后端（不装 pytest 等测试工具）
COPY requirements-app.txt requirements-mem0.txt ./
RUN pip install --no-cache-dir -r requirements-app.txt -r requirements-mem0.txt

COPY app ./app

# 非 root 运行（P2 审查）：容器内进程无需写应用目录，只可能写运行时
# 临时目录；显式收敛属主，避免以 root 跑业务代码。
# home 必须真实存在且归 appuser（P1-1）：mem0ai 在 $HOME/.mem0/ 下写
# history.db，--no-create-home 时该目录不存在/不可写，首个记忆操作就
# PermissionError
RUN useradd --system --uid 10001 --home-dir /home/appuser --create-home appuser \
    && chown -R appuser:appuser /srv/app
USER appuser

ENV PYTHONUNBUFFERED=1
# 同 P1-1：显式钉住 HOME/MEM0_DIR——system 用户在部分基础镜像里 HOME 未设
# 或指向不可写路径，mem0 的本地历史库落在 $MEM0_DIR（缺省 ~/.mem0）下
ENV HOME=/home/appuser
ENV MEM0_DIR=/home/appuser/.mem0

# api 默认入口；worker 用 compose 覆盖 command。
# --no-access-log（INV-14，threads9）：请求行含完整 query（用户内容），
# 与 docker-compose.yml 的 api command 保持一致
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log"]
