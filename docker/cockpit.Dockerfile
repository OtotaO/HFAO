FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --all-extras

COPY packages/hfao packages/hfao
COPY apps/cockpit apps/cockpit

EXPOSE 7860

ENTRYPOINT ["uv", "run", "python", "-m", "hfao.cli", "up"]
