FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# PyPI 源。默认走国内镜像（DH2600 所在网络 pypi.org 不通）；
# CI 或其他环境可用 --build-arg 覆盖。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

WORKDIR /app

COPY requirements.txt .

# PIP_TRUSTED_HOST 允许为空（例如直连 pypi.org 时不需要）
RUN set -eux; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then \
      pip install --no-cache-dir \
        --index-url "$PIP_INDEX_URL" \
        --trusted-host "$PIP_TRUSTED_HOST" \
        -r requirements.txt; \
    else \
      pip install --no-cache-dir \
        --index-url "$PIP_INDEX_URL" \
        -r requirements.txt; \
    fi

COPY app/ /app/

EXPOSE 2600

# 容器内以 root 运行：/proc/it86/fan 是 0222，读转速需要 CAP_DAC_OVERRIDE
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "2600", "--no-access-log"]
