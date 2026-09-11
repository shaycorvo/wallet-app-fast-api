import json
import logging

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

REQUESTS = Counter("wallet_http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("wallet_http_request_duration_seconds", "HTTP request duration", ["method", "path"])
TRANSFERS = Counter("wallet_transfers_total", "Transfers", ["status"])
TRANSFERS_CREATED = Counter("wallet_transfer_creations_total", "New transfer requests")
TRANSFERS_DECLINED = Counter("wallet_transfers_declined_total", "Declined transfers", ["reason"])
REPLAYS = Counter("wallet_idempotent_replays_total", "Idempotent transfer replays")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"level": record.levelname.lower(), "event": record.getMessage()}
        for field in ("correlation_id", "transfer_id", "status", "amount_paise", "duration_ms"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("wallet_service")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)