FROM python:3.13-slim

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY . .
RUN uv sync --locked --no-dev
EXPOSE 10000
CMD ["sh", "-c", "uv run --no-dev -- uvicorn --host 0.0.0.0 --port ${PORT:-10000} fresco.main:app"]
