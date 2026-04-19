FROM python:3.10-slim

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --all-extras

COPY packages/hfao packages/hfao

ENTRYPOINT ["uv", "run", "python", "-m", "hfao.cli", "worker"]
