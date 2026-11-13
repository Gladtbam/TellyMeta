FROM python:slim-trixie AS builder

ARG DB_EXTRA=all

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN if [ "$DB_EXTRA" = "all" ]; then \
        uv sync --frozen --no-dev --no-install-project --compile-bytecode --all-extras; \
    else \
        uv sync --frozen --no-dev --no-install-project --compile-bytecode --extra "$DB_EXTRA"; \
    fi



FROM python:slim-trixie

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    mkvtoolnix \
    curl \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv

COPY . .

RUN mkdir -p /var/lib/tellymeta /var/log/tellymeta

VOLUME ["/var/lib/tellymeta", "/var/log/tellymeta"]

EXPOSE 5080

CMD ["python", "main.py"]