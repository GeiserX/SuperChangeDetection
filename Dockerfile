FROM python:3.14.8-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY superchangedetection ./superchangedetection
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH SCD_DATA=/data HOME=/tmp
USER 1000:1000
CMD ["scd", "check"]
