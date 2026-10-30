"""M17 diagnostics API tests: lookup, errors, compatibility, immutability."""

import copy
import hashlib
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


def _trial(scenario, trial_id, index=1, **overrides):
    trial = {
        "scenario": scenario,
        "mode": "live",
        "trial_index": index,
        "trial_id": trial_id,
        "outcome": "failed",
        "error": None,
        "turns": 2,
        "successful_charges": 2,
        "order_ids": [1],
        "payment_ids": [1, 2],
        "violations": [{"code": "multiple_successful_charges_for_order", "message": "m"}],
        "usage": {},
        "trace": [
            {"sequence": 1, "kind": "tool", "action": "attempt_payment",
             "request": {"body": {"idempotency_key": "orig"}}, "http_status": 504},
            {"sequence": 2, "kind": "tool", "action": "retry_payment",
             "request": {"body": {"idempotency_key": "retry"}}, "http_status": 200},
        ],
        "evaluation_evidence": {"valid": False, "violations": []},
        "task": {"original_idempotency_key": "orig"},
    }
    trial.update(overrides)
    return trial


def _orchestrated(trials_by_scenario):
    scenarios = []
    for scenario, trials in trials_by_scenario:
        scenarios.append({
            "scenario": scenario, "scenario_key": "k", "mode": "live", "status": "completed",
            "error": None, "trials_requested": len(trials), "trials_completed": len(trials),
            "passed": 0, "failed": len(trials), "errors": 0, "pass_rate": 0.0,
            "turns_total": 0, "usage": {}, "violation_counts": {},
            "baselines": [], "trials": trials,
        })
    return {
        "experiment": "agentcrash-orchestrated-run", "mode": "live", "model": "m",
        "requested_trials": 1, "scenarios_requested": len(scenarios),
        "scenarios_completed": len(scenarios), "aggregate": {}, "scenarios": scenarios,
        "cost_note": "cost_not_calculated (no reliable pricing data)",
    }


def _write(directory: Path, name: str, doc: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


async def test_report_level_lists_all(client, artifact_dir: Path):
    doc = _orchestrated([("s1", [_trial("s1", "a", 1), _trial("s1", "b", 2)])])
    _write(artifact_dir, "r.json", doc)
    response = await client.get("/api/reports/r.json/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics_version"] == "m17/v1"
    assert len(body["diagnostics"]) == 2


async def test_trial_lookup_and_404(client, artifact_dir: Path):
    doc = _orchestrated([("s1", [_trial("s1", "a", 1)])])
    _write(artifact_dir, "r.json", doc)
    ok = await client.get("/api/reports/r.json/diagnostics", params={"scenario": "s1", "trial_id": "a"})
    assert ok.status_code == 200
    assert ok.json()["trial_ref"]["trial_id"] == "a"
    missing = await client.get("/api/reports/r.json/diagnostics", params={"scenario": "s1", "trial_id": "zzz"})
    assert missing.status_code == 404


async def test_multiple_matches_409(client, artifact_dir: Path):
    doc = _orchestrated([
        ("s1", [_trial("s1", "dup", 1)]),
        ("s2", [_trial("s2", "dup", 1)]),
    ])
    _write(artifact_dir, "r.json", doc)
    response = await client.get("/api/reports/r.json/diagnostics", params={"trial_id": "dup"})
    assert response.status_code == 409


async def test_error_contracts_preserved(client, artifact_dir: Path):
    (artifact_dir / "note.md").write_text("hello", encoding="utf-8")
    markdown = await client.get("/api/reports/note.md/diagnostics")
    assert markdown.status_code == 404
    (artifact_dir / "bad.json").write_text("{not json", encoding="utf-8")
    invalid = await client.get("/api/reports/bad.json/diagnostics")
    assert invalid.status_code == 422
    missing = await client.get("/api/reports/nope.json/diagnostics")
    assert missing.status_code == 404


async def test_artifacts_immutable(client, artifact_dir: Path):
    doc = _orchestrated([("s1", [_trial("s1", "a", 1)])])
    path = _write(artifact_dir, "r.json", doc)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    response = await client.get("/api/reports/r.json/diagnostics")
    assert response.status_code == 200
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after
    assert json.loads(path.read_text(encoding="utf-8")) == doc


async def test_legacy_deterministic_comparison_unknown(client, artifact_dir: Path):
    doc = {
        "scenario": "payment_timeout_after_commit",
        "results": [
            {"strategy_name": "unsafe_retry", "task_outcome": "failed",
             "successful_charge_count": 2,
             "invariant_violations": [{"code": "multiple_successful_charges_for_order", "message": "m"}],
             "trace": []},
        ],
    }
    # Bypass _detect_kind gate by writing through the orchestrated path is not
    # possible here; call diagnostics directly for legacy shapes.
    from agentcrash.experiments.diagnostics import build_report_diagnostics
    diagnostics = build_report_diagnostics(copy.deepcopy(doc))
    assert diagnostics[0]["axes"]["evidence_sufficiency"] == "insufficient"
    assert diagnostics[0]["recovery"]["status"] == "unknown"
