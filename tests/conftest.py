from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agentcrash.api import create_app


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    database_path = tmp_path / "test.db"
    return create_app(f"sqlite:///{database_path}")


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def order_payload() -> dict[str, object]:
    return {"item": "widget", "quantity": 2, "unit_price": "12.50"}


async def create_stocked_order(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> dict[str, object]:
    inventory_response = await client.post(
        "/inventory", json={"item": "widget", "quantity_on_hand": 10}
    )
    assert inventory_response.status_code == 201
    order_response = await client.post("/orders", json=order_payload)
    assert order_response.status_code == 201
    return order_response.json()
