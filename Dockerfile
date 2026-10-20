FROM python:3.14.8-slim
# The base image ships ca-certificates 20250419, which still trusts the compromised eMudhra
# intermediates; trixie-security carries the bundle that distrusts them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY superchangedetection ./superchangedetection
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH SCD_DATA=/data HOME=/tmp
USER 1000:1000
CMD ["scd", "run"]
