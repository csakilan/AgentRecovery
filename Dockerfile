FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# The virtualenv and bytecode are built as root and only read at runtime, so an unprivileged
# user can run the services without owning /app.
RUN useradd --system --uid 10001 --no-create-home simulator
USER simulator
CMD ["python", "-m", "lab.simulator", "public"]
