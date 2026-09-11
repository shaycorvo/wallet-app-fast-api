FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

RUN groupadd --system app && useradd --system --gid app app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY src ./src
COPY scripts ./scripts

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/live')"
CMD ["sh", "-c", "python -m scripts.init_db && uvicorn src.main:app --host 0.0.0.0 --port 8000"]