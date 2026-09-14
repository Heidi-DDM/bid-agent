# 运行时镜像（api / worker / migrate 共用）—— 见 docs/11-部署方案-阿里云演示环境.md §4
# 构建上下文 = 仓库根目录：docker build -f deploy/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    RUNTIME_TESSERACT=/usr/bin/tesseract \
    RUNTIME_PDFTOTEXT=/usr/bin/pdftotext \
    RUNTIME_PDFTOPPM=/usr/bin/pdftoppm

# 解析/OCR 依赖（F018 §2）：pdftotext/pdftoppm、tesseract chi_sim；curl 供 compose 健康检查
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr tesseract-ocr-chi-sim libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements-server.txt /tmp/requirements-server.txt
RUN pip install -r /tmp/requirements-server.txt

# 仅复制运行所需代码；企业资料/原文/.env 由 .dockerignore 与 rsync 排除双重保证不进镜像
COPY alembic.ini pytest.ini ./
COPY runtime/ runtime/
COPY scripts/matching/ scripts/matching/
COPY scripts/setup_rbac.sql scripts/setup_rbac.sql

# 语法门禁（F018 §8：py_compile 可执行）
RUN python -m compileall -q runtime scripts/matching

# 非 root 运行；/data/objects 为 OBJECT_STORE_ROOT 挂载点
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data/objects \
    && chown -R app:app /data /app
USER app

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "runtime.api:app", "--host", "0.0.0.0", "--port", "8000"]
