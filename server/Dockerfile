FROM python:3.12-slim

WORKDIR /srv/app

# 生产镜像默认 MEMORY_BACKEND=mem0，mem0ai 必须随镜像安装，
# 否则启动直接报 ImportError（不会静默退化为 inmemory）
COPY requirements.txt requirements-mem0.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-mem0.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1

# api 默认入口；worker 用 compose 覆盖 command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
