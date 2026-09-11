import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from src.database import SessionFactory, close_database
from src.observability import LATENCY, REQUESTS, configure_logging, metrics_response
from src.test_support.router import router as test_support_router
from src.transfers.router import router as transfers_router
from src.wallets.router import router as wallets_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_database()


app = FastAPI(title="Wallet Service", lifespan=lifespan)
configure_logging()
app.include_router(wallets_router)
app.include_router(transfers_router)
app.include_router(test_support_router)
logger = logging.getLogger("wallet_service")


def metric_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def record_request(request: Request, status_code: int, duration_ms: float) -> None:
    route_path = metric_path(request)
    REQUESTS.labels(request.method, route_path, status_code).inc()
    LATENCY.labels(request.method, route_path).observe(duration_ms / 1000)


@app.middleware("http")
async def correlation_logging(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = correlation_id
    start = time.perf_counter()
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    try:
        response = await call_next(request)
    except (OperationalError, SQLAlchemyTimeoutError):
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        record_request(request, status_code=503, duration_ms=duration_ms)
        logger.warning(
            "database_unavailable",
            extra={"correlation_id": correlation_id, "duration_ms": duration_ms},
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "Service temporarily unavailable"},
            headers={"Retry-After": "1", "X-Correlation-ID": correlation_id},
        )
    except DBAPIError as error:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate in {"40P01", "55P03", "57014"}:
            record_request(request, status_code=503, duration_ms=duration_ms)
            logger.warning(
                "database_busy",
                extra={"correlation_id": correlation_id, "duration_ms": duration_ms},
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "Service temporarily unavailable"},
                headers={"Retry-After": "1", "X-Correlation-ID": correlation_id},
            )
        record_request(request, status_code=500, duration_ms=duration_ms)
        logger.exception(
            "request_failed",
            extra={"correlation_id": correlation_id, "duration_ms": duration_ms},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers={"X-Correlation-ID": correlation_id},
        )
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        record_request(request, status_code=500, duration_ms=duration_ms)
        logger.exception(
            "request_failed",
            extra={"correlation_id": correlation_id, "duration_ms": duration_ms},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers={"X-Correlation-ID": correlation_id},
        )
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Correlation-ID"] = correlation_id
    record_request(request, status_code=response.status_code, duration_ms=duration_ms)
    logger.info("request_completed", extra={"correlation_id": correlation_id, "duration_ms": duration_ms})
    return response


@app.get("/live", include_in_schema=False)
async def live() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/health", include_in_schema=False)
@app.get("/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    async with SessionFactory() as session:
        await session.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok"})


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return metrics_response()