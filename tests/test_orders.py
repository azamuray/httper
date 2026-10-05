import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.parser import text_string_to_metric_families

from app.main import Metrics, create_app

ORDER: dict[str, str | int] = {
    "order_id": "order-1",
    "customer_id": "customer-1",
    "amount": 50,
    "currency": "USD",
}


async def instant() -> None:
    pass


@pytest.fixture
def application() -> FastAPI:
    return create_app(instant)


@pytest.fixture
async def client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as value:
        yield value


async def test_create_get_and_duplicate(client: AsyncClient) -> None:
    response = await client.post("/orders", json=ORDER)
    assert response.status_code == 201
    assert response.json() == {**ORDER, "amount": "50"}
    fetched = await client.get("/orders/order-1")
    assert fetched.status_code == 200
    assert fetched.json() == response.json()
    assert (await client.post("/orders", json=ORDER)).status_code == 409
    assert (await client.get("/orders/missing")).status_code == 404


async def test_negative_amount(client: AsyncClient, application: FastAPI) -> None:
    response = await client.post("/orders", json={**ORDER, "amount": -1})
    assert response.status_code == 400
    metrics = cast(Metrics, application.state.metrics)
    assert metrics.registry.get_sample_value("orders_validation_errors_total") == 1
    assert metrics.registry.get_sample_value("orders_created_total") == 0
    assert (await client.get("/orders/order-1")).status_code == 404


@pytest.mark.parametrize("amount", [0, "12.34"])
async def test_valid_amount(client: AsyncClient, amount: int | str) -> None:
    assert (await client.post("/orders", json={**ORDER, "amount": amount})).status_code == 201


@pytest.mark.parametrize(
    "override",
    [
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "invalid"},
        {"order_id": " "},
        {"customer_id": 123},
        {"currency": "usd"},
        {"extra": 1},
    ],
)
async def test_invalid_payload(client: AsyncClient, override: dict[str, str | int]) -> None:
    assert (await client.post("/orders", json={**ORDER, **override})).status_code == 422


async def test_missing_fields(client: AsyncClient) -> None:
    assert (await client.post("/orders", json={})).status_code == 422


async def test_metrics(client: AsyncClient, application: FastAPI) -> None:
    await client.post("/orders", json=ORDER)
    await client.get("/orders/order-1")
    await client.get("/orders/another")
    await client.get("/unknown/1")
    await client.get("/unknown/2")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    families = {family.name for family in text_string_to_metric_families(response.text)}
    assert "orders_amount_sum" in families
    assert "# TYPE orders_amount_sum histogram" in response.text
    metrics = cast(Metrics, application.state.metrics)
    registry = metrics.registry
    assert registry.get_sample_value("orders_created_total") == 1
    assert registry.get_sample_value("orders_amount_sum_count") == 1
    assert registry.get_sample_value("orders_amount_sum_sum") == 50
    for boundary, count in [
        ("10.0", 0),
        ("50.0", 1),
        ("100.0", 1),
        ("500.0", 1),
        ("1000.0", 1),
        ("+Inf", 1),
    ]:
        assert registry.get_sample_value("orders_amount_sum_bucket", {"le": boundary}) == count
    assert registry.get_sample_value("orders_in_progress") == 0
    assert (
        registry.get_sample_value(
            "request_duration_seconds_count",
            {"method": "GET", "endpoint": "/orders/{order_id}"},
        )
        == 2
    )
    assert (
        registry.get_sample_value(
            "request_duration_seconds_count",
            {"method": "GET", "endpoint": "unmatched"},
        )
        == 2
    )
    assert (
        registry.get_sample_value(
            "request_duration_seconds_count",
            {"method": "GET", "endpoint": "/metrics"},
        )
        == 1
    )


async def test_concurrent_duplicate_and_gauge() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def processor() -> None:
        entered.set()
        await release.wait()

    application = create_app(processor)
    metrics = cast(Metrics, application.state.metrics)
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        task = asyncio.create_task(client.post("/orders", json=ORDER))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            assert metrics.registry.get_sample_value("orders_in_progress") == 1
            assert (await client.get("/orders/order-1")).status_code == 404
            assert (await client.post("/orders", json=ORDER)).status_code == 409
        finally:
            release.set()
        assert (await task).status_code == 201
    assert metrics.registry.get_sample_value("orders_in_progress") == 0
    assert metrics.registry.get_sample_value("orders_created_total") == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_cleanup(cancel: bool) -> None:
    entered = asyncio.Event()
    attempts = 0

    async def processor() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("Processing failed")

    application = create_app(processor)
    metrics = cast(Metrics, application.state.metrics)
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        task = asyncio.create_task(client.post("/orders", json=ORDER))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).status_code == 500
        assert metrics.registry.get_sample_value("orders_in_progress") == 0
        assert metrics.registry.get_sample_value("orders_created_total") == 0
        assert (await client.get("/orders/order-1")).status_code == 404
        assert (await client.post("/orders", json=ORDER)).status_code == 201
