import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.types import ASGIApp, Receive, Scope, Send

Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
Currency = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]


class Order(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid", frozen=True)

    order_id: Identifier
    customer_id: Identifier
    amount: Decimal
    currency: Currency


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.created = Counter(
            "orders_created_total", "Successfully created orders", registry=self.registry
        )
        self.amount = Histogram(
            "orders_amount_sum",
            "Amounts of created orders across all currencies",
            buckets=(10, 50, 100, 500, 1000),
            registry=self.registry,
        )
        self.duration = Histogram(
            "request_duration_seconds",
            "HTTP request duration in seconds",
            labelnames=("method", "endpoint"),
            registry=self.registry,
        )
        self.in_progress = Gauge(
            "orders_in_progress", "Orders currently processing", registry=self.registry
        )
        self.validation_errors = Counter(
            "orders_validation_errors_total",
            "Orders rejected for negative amounts",
            registry=self.registry,
        )


class DurationMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send)
        finally:
            route = scope.get("route")
            endpoint = getattr(route, "path", "unmatched")
            self.metrics.duration.labels(method=scope["method"], endpoint=endpoint).observe(
                time.perf_counter() - started
            )


async def process_order() -> None:
    await asyncio.sleep(random.uniform(0.05, 0.5))


def create_app(processor: Callable[[], Awaitable[None]] = process_order) -> FastAPI:
    application = FastAPI(title="Orders API", version="0.1.0")
    metrics = Metrics()
    orders: dict[str, Order] = {}
    pending: set[str] = set()
    application.state.metrics = metrics
    application.add_middleware(DurationMiddleware, metrics=metrics)

    @application.post("/orders", status_code=201, response_model=Order)
    async def create_order(order: Order) -> Order:
        if order.amount < 0:
            metrics.validation_errors.inc()
            raise HTTPException(status_code=400, detail="amount must be non-negative")
        if order.order_id in orders or order.order_id in pending:
            raise HTTPException(status_code=409, detail="order_id already exists or is processing")
        # No await between checking and reserving: atomic within this process's event loop.
        pending.add(order.order_id)
        metrics.in_progress.inc()
        try:
            await processor()
            orders[order.order_id] = order
            metrics.created.inc()
            metrics.amount.observe(float(order.amount))
            return order
        finally:
            pending.remove(order.order_id)
            metrics.in_progress.dec()

    @application.get("/orders/{order_id}", response_model=Order)
    async def get_order(order_id: str) -> Order:
        if order_id not in orders:
            raise HTTPException(status_code=404, detail="Order not found")
        return orders[order_id]

    @application.get("/metrics", response_class=Response)
    async def get_metrics() -> Response:
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    return application


app = create_app()
