"""Offline tests for POST /api/series (M16).

All fixtures are built in tmp directories; historical artifacts are never
touched. No live calls, no credentials.
"""

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agentcrash.api import create_app


@pytest.fixture
def artifact_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "artifacts"
    directory.mkdir()
    monkeypatch.setenv("AGENTCRASH_ARTIFACTS_DIR", str(directory))
    return directory


@pytest.fixture
def app(tmp_path: Path, artifact_dir: Path) -> FastAPI:
    return create_app(f"sqlite:///{tmp_path / 'test.db'}")


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _trace(n_calls=1):
    events = [
        {"sequence": 1, "kind": "model_request"},
        {"sequence": 2, "kind": "model_response"},
    ]
    seq = 3
    for _ in range(n_calls):
        events.append({"sequence": seq, "kind": "tool_call"})
        seq += 1
        events.append({"sequence": seq, "kind": "tool_result"})
        seq += 1
    return events


def _trial(scenario, outcome="passed", **overrides):
    trial = {
        "scenario": scenario,
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": outcome,
        "error": None,
        "turns": 2,
        "successful_charges": 1,
        "payment_ids": [1],
        "order_ids": [],
        "violations": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "responses": 1,
        },
        "trace": _trace(),
        "evaluation_evidence": {
            "valid": outcome == "passed",
            "source": "persisted-state-evaluation",
        },
    }
    trial.update(overrides)
    return trial


PAY = "payment_timeout_after_commit"


def _write(directory: Path, name: str, trials) -> Path:
    report = {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "live",
        "model": "fake-model",
        "scenarios": [
            {
                "scenario": PAY,
                "mode": "live",
                "trials": trials,
                "trials_requested": len(trials),
            }
        ],
    }
    path = directory / name
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _pay_pass(**kw):
    return _trial(PAY, "passed", **kw)


def _pay_fail_dup(**kw):
    kw.setdefault("charges", 2)
    kw.setdefault("payment_ids", [1, 2])
    kw.setdefault("violations", [{"code": "multiple_successful_charges_for_order"}])
    return _trial(PAY, "failed", **kw)


async def test_series_happy_path_with_strategy_id(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass(), _pay_pass()])
    _write(artifact_dir, "m1.json", [_pay_fail_dup(), _pay_pass()])
    _write(artifact_dir, "m2.json", [_pay_pass(), _pay_pass()])
    response = await client.post(
        "/api/series",
        json={
            "baseline": "b.json",
            "members": ["m1.json", "m2.json"],
            "strategy_id": "s",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series_trend_version"] == 1
    assert body["baseline"]["strategy_id"] == "s"
    assert body["series"]["member_count"] == 2
    assert body["series"]["eligible_count"] == 2
    assert [m["source"] for m in body["members"]] == ["m1.json", "m2.json"]
    assert all(m["eligible"] for m in body["members"])
    assert body["series"]["regressions_present"] is True
    assert body["series"]["members_regressed_vs_baseline"] == ["m1.json"]
    assert body["cost"] == "cost_not_calculated (no reliable pricing data)"
    assert "ranking" not in body and "winner" not in body
    assert "ranking" not in body["series"] and "winner" not in body["series"]
    for member in body["members"]:
        assert "ranking" not in member and "winner" not in member


async def test_series_requires_strategy_labels(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    _write(artifact_dir, "m.json", [_pay_pass()])
    response = await client.post(
        "/api/series", json={"baseline": "b.json", "members": ["m.json"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series"]["eligible_count"] == 0
    assert any(
        "unconfirmed configuration" in r for r in body["members"][0]["reasons"]
    )


async def test_series_whitespace_strategy_id_is_ineligible(
    client, artifact_dir: Path
):
    _write(artifact_dir, "b.json", [_pay_pass()])
    _write(artifact_dir, "m.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={"baseline": "b.json", "members": ["m.json"], "strategy_id": "   "},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series"]["eligible_count"] == 0
    assert any(
        "unconfirmed configuration" in r for r in body["members"][0]["reasons"]
    )


async def test_series_whitespace_override_falls_back_to_common(
    client, artifact_dir: Path
):
    _write(artifact_dir, "b.json", [_pay_pass()])
    _write(artifact_dir, "m.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={
            "baseline": "b.json",
            "members": ["m.json"],
            "strategy_id": "s",
            "strategy_ids": {"m.json": "  "},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series"]["eligible_count"] == 1
    assert body["members"][0]["eligible"] is True


async def test_series_per_artifact_strategy_ids(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    _write(artifact_dir, "m1.json", [_pay_pass()])
    _write(artifact_dir, "m2.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={
            "baseline": "b.json",
            "members": ["m1.json", "m2.json"],
            "strategy_ids": {"b.json": "s", "m1.json": "s", "m2.json": "other"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    by_name = {m["source"]: m for m in body["members"]}
    assert by_name["m1.json"]["eligible"] is True
    assert by_name["m2.json"]["eligible"] is False


async def test_series_empty_members_rejected(client):
    response = await client.post(
        "/api/series", json={"baseline": "b.json", "members": []}
    )
    assert response.status_code == 400


async def test_series_more_than_50_members_rejected(
    client, artifact_dir: Path
):
    _write(artifact_dir, "b.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={"baseline": "b.json", "members": [f"m{i}.json" for i in range(51)]},
    )
    assert response.status_code == 400


async def test_series_deduplicates_members(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    _write(artifact_dir, "m.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={
            "baseline": "b.json",
            "members": ["m.json", "m.json"],
            "strategy_id": "s",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series"]["member_count"] == 1
    assert any("duplicate member" in w for w in body["warnings"])


async def test_series_traversal_protection(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={"baseline": "b.json", "members": ["../secret.json"]},
    )
    assert response.status_code == 404


async def test_series_missing_member_is_404(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    response = await client.post(
        "/api/series", json={"baseline": "b.json", "members": ["absent.json"]}
    )
    assert response.status_code == 404


async def test_series_malformed_member_is_422(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    (artifact_dir / "bad.json").write_text("{not json", encoding="utf-8")
    response = await client.post(
        "/api/series", json={"baseline": "b.json", "members": ["bad.json"]}
    )
    assert response.status_code == 422


async def test_series_oversized_member_is_not_read(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass()])
    big = artifact_dir / "big.json"
    big.write_text("{" + " " * (10_000_100), encoding="utf-8")
    response = await client.post(
        "/api/series", json={"baseline": "b.json", "members": ["big.json"]}
    )
    assert response.status_code == 404


async def test_series_incompatible_member_stays_visible(client, artifact_dir: Path):
    _write(artifact_dir, "b.json", [_pay_pass(), _pay_pass()])
    _write(artifact_dir, "m.json", [_pay_pass()])
    response = await client.post(
        "/api/series",
        json={"baseline": "b.json", "members": ["m.json"], "strategy_id": "s"},
    )
    assert response.status_code == 200
    body = response.json()
    entry = body["members"][0]
    assert entry["eligible"] is False
    assert entry["reasons"]
    assert body["series"]["eligible_count"] == 0
    assert body["series"]["regressions_present"] is False
