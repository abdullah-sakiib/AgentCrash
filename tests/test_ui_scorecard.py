"""Offline tests for the read-only scorecard/comparison API (M13 Phase A).

All fixtures are built in tmp directories; historical artifacts are never
touched. No live calls, no credentials.
"""

import copy
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agentcrash.api import create_app
from agentcrash.experiments import scorecard as sc


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


def _orchestrated(entries, mode="live", model="fake-model"):
    return {
        "experiment": "agentcrash-orchestrated-run",
        "mode": mode,
        "model": model,
        "scenarios": [
            {
                "scenario": name,
                "mode": mode,
                "trials": trials,
                "trials_requested": len(trials),
            }
            for name, trials in entries
        ],
    }


def _write(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


PAY = "payment_timeout_after_commit"
INTENT = "duplicate_order_for_intent"


async def test_scorecard_matches_pure_function(client, artifact_dir: Path):
    report = _orchestrated([(PAY, [_trial(PAY)])])
    _write(artifact_dir, "r.json", report)
    response = await client.get("/api/reports/r.json/scorecard")
    assert response.status_code == 200
    expected = sc.build_scorecard_document(report, source="r.json")
    assert response.json() == expected


async def test_scorecard_strategy_id_passthrough(client, artifact_dir: Path):
    _write(artifact_dir, "r.json", _orchestrated([(PAY, [_trial(PAY)])]))
    response = await client.get(
        "/api/reports/r.json/scorecard?strategy_id=demo"
    )
    assert response.status_code == 200
    assert response.json()["scope"]["strategy_id"] == "demo"


async def test_scorecard_rejects_unscorable_input(client, artifact_dir: Path):
    # A supported legacy single-trial report is not a scorecard input.
    single = {
        "scenario": PAY,
        "model": "m",
        "trial_count": 1,
        "live_outcome": "passed",
        "live_error": None,
        "order_id": 1,
        "payment_ids": [1],
        "successful_charge_count": 1,
        "turns_used": 2,
        "usage": {},
        "invariant_violations": [],
        "baselines": [],
        "trace": [],
    }
    _write(artifact_dir, "single.json", single)
    listing = await client.get("/api/reports")
    by_name = {e["name"]: e for e in listing.json()["entries"]}
    assert by_name["single.json"]["supported"] is True
    response = await client.get("/api/reports/single.json/scorecard")
    assert response.status_code == 422
    assert "cannot be scored" in response.json()["detail"]


async def test_scorecard_missing_and_malformed(client, artifact_dir: Path):
    assert (await client.get("/api/reports/absent.json/scorecard")).status_code == 404
    (artifact_dir / "bad.json").write_text("{oops", encoding="utf-8")
    assert (await client.get("/api/reports/bad.json/scorecard")).status_code == 422


async def test_comparison_compatible_matches_pure_function(
    client, artifact_dir: Path
):
    base = _orchestrated([(PAY, [_trial(PAY, outcome="failed",
                                        violations=[{"code": "multiple_successful_charges_for_order"}],
                                        charges=2),
                                 _trial(PAY)])])
    cand = _orchestrated([(PAY, [_trial(PAY), _trial(PAY)])])
    _write(artifact_dir, "b.json", base)
    _write(artifact_dir, "c.json", cand)
    response = await client.get("/api/comparisons?baseline=b.json&candidate=c.json")
    assert response.status_code == 200
    body = response.json()
    expected = sc.compare_scorecard_documents(
        sc.build_scorecard_document(base, source="b.json"),
        sc.build_scorecard_document(cand, source="c.json"),
    )
    assert body == expected
    assert body["compatible"] is True
    assert body["overall"]["recovery_success_rate"]["direction"] == "improved"
    assert "winner" not in json.dumps(body)
    assert "ranking" not in json.dumps(body)


async def test_comparison_incompatible_is_normal_result(
    client, artifact_dir: Path
):
    _write(artifact_dir, "b.json", _orchestrated([(PAY, [_trial(PAY)])]))
    _write(artifact_dir, "c.json", _orchestrated([(INTENT, [_trial(INTENT)])]))
    response = await client.get("/api/comparisons?baseline=b.json&candidate=c.json")
    assert response.status_code == 200
    body = response.json()
    assert body["compatible"] is False
    assert body["reasons"]
    assert body["overall"] == {}
    assert body["scenarios"] == {}


async def test_comparison_traversal_protection(client, artifact_dir: Path):
    _write(artifact_dir, "c.json", _orchestrated([(PAY, [_trial(PAY)])]))
    for params in [
        "baseline=..%2Fsecret.json&candidate=c.json",
        "baseline=c.json&candidate=%2Fetc%2Fpasswd",
        "baseline=..%2F..%2Fc.json&candidate=c.json",
    ]:
        (artifact_dir / "secret.json").write_text("{}", encoding="utf-8")
        response = await client.get(f"/api/comparisons?{params}")
        assert response.status_code == 404, params
    # Dot segments that normalize inside the directory remain servable.
    inside = await client.get(
        "/api/comparisons?baseline=c.json&candidate=sub%2F..%2Fc.json"
    )
    assert inside.status_code == 200
    assert inside.json()["compatible"] is True
    missing = await client.get("/api/comparisons?baseline=absent.json&candidate=c.json")
    assert missing.status_code == 404
