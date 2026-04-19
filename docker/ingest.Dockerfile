FROM python:3.10-slim

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --all-extras

COPY packages/hfao packages/hfao

EXPOSE 4318

ENTRYPOINT ["uv", "run", "python", "-m", "hfao.ingest.server"]
