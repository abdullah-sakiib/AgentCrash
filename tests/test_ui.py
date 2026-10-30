"""Offline tests for the read-only UI artifact API (M13 Phase A).

All artifact files live in pytest tmp directories; historical artifacts
under artifacts/ are never touched. No live calls, no credentials.
"""

import json
import os
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


def _orchestrated_report(name="payment_timeout_after_commit") -> dict:
    return {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "deterministic",
        "model": None,
        "requested_trials": 2,
        "scenarios_requested": 1,
        "scenarios_completed": 1,
        "aggregate": {},
        "scenarios": [
            {
                "scenario": name,
                "scenario_key": "payment-timeout",
                "mode": "deterministic",
                "status": "completed",
                "error": None,
                "trials_requested": 2,
                "trials_completed": 2,
                "passed": 1,
                "failed": 1,
                "errors": 0,
                "pass_rate": 0.5,
                "turns_total": 4,
                "usage": {},
                "violation_counts": {},
                "baselines": [],
                "trials": [],
            }
        ],
        "cost_note": "cost_not_calculated (no reliable pricing data)",
    }


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


async def test_health_reports_readability(client, artifact_dir: Path):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["artifacts_readable"] is True
    assert "artifacts" not in json.dumps(body).lower().replace(
        "artifacts_readable", ""
    ).replace(str(artifact_dir).lower(), "")


async def test_health_unreadable_dir(client, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGENTCRASH_ARTIFACTS_DIR", str(tmp_path / "absent"))
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["artifacts_readable"] is False


async def test_scenarios_come_from_project_constants(client):
    from agentcrash.experiments.run_experiment import SUPPORTED_SCENARIOS
    from agentcrash.experiments.scorecard import DEFAULT_FAULTS

    response = await client.get("/api/scenarios")
    assert response.status_code == 200
    scenarios = response.json()["scenarios"]
    assert [s["key"] for s in scenarios] == list(SUPPORTED_SCENARIOS)
    assert len(scenarios) == 3
    by_key = {s["key"]: s for s in scenarios}
    assert by_key["payment-timeout"]["canonical_name"] == (
        "payment_timeout_after_commit"
    )
    assert by_key["payment-timeout"]["fault"] == dict(
        DEFAULT_FAULTS["payment_timeout_after_commit"]
    )
    assert by_key["payment-timeout"]["strategies"] == {
        "unsafe": "unsafe_retry",
        "safe": "status_check",
    }
    assert "fulfill_without_payment" in by_key["fulfill-before-payment"]["aliases"]
    assert by_key["fulfill-before-payment"]["fault"] == {"fault_type": "none"}
    for scenario in scenarios:
        assert scenario["invariants"]
        assert scenario["description"]


async def test_listing_tolerates_every_artifact_shape(
    client, artifact_dir: Path
):
    _write(artifact_dir, "good.json", json.dumps(_orchestrated_report()))
    _write(artifact_dir, "notes.md", "# notes\n")
    _write(artifact_dir, "broken.json", "{not json")
    _write(artifact_dir, "weird.json", json.dumps({"hello": "world"}))
    (artifact_dir / "subdir").mkdir()

    response = await client.get("/api/reports")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["good.json"]["kind"] == "orchestrated_run"
    assert by_name["good.json"]["supported"] is True
    assert by_name["notes.md"]["kind"] == "markdown"
    assert by_name["notes.md"]["supported"] is False
    assert by_name["broken.json"]["supported"] is False
    assert by_name["weird.json"]["supported"] is False
    assert "subdir" not in by_name
    for entry in body["entries"]:
        assert set(entry) == {"name", "size_bytes", "mtime", "kind", "supported"}
        assert "/" not in entry["name"] and "\\" not in entry["name"]


async def test_listing_newest_first_with_pagination(
    client, artifact_dir: Path
):
    import time

    for i in range(5):
        _write(artifact_dir, f"r{i}.json", json.dumps(_orchestrated_report()))
        # Ensure distinct mtimes even on coarse filesystems.
        time.sleep(0.02)
        path = artifact_dir / f"r{i}.json"
        os.utime(path, (1000 + i, 1000 + i))

    first = await client.get("/api/reports?limit=2&offset=0")
    assert [e["name"] for e in first.json()["entries"]] == ["r4.json", "r3.json"]
    assert first.json()["total"] == 5
    second = await client.get("/api/reports?limit=2&offset=2")
    assert [e["name"] for e in second.json()["entries"]] == ["r2.json", "r1.json"]
    bad = await client.get("/api/reports?limit=0")
    assert bad.status_code == 400
    bad = await client.get("/api/reports?limit=1001")
    assert bad.status_code == 400
    bad = await client.get("/api/reports?offset=-1")
    assert bad.status_code == 400


async def test_retrieval_returns_parsed_json(client, artifact_dir: Path):
    report = _orchestrated_report()
    _write(artifact_dir, "run.json", json.dumps(report))
    response = await client.get("/api/reports/run.json")
    assert response.status_code == 200
    assert response.json() == report


async def test_missing_file_is_404(client):
    response = await client.get("/api/reports/absent.json")
    assert response.status_code == 404


async def test_unsupported_shapes_are_not_served_as_reports(
    client, artifact_dir: Path
):
    _write(artifact_dir, "notes.md", "# notes\n")
    _write(artifact_dir, "weird.json", json.dumps({"hello": "world"}))
    _write(artifact_dir, "broken.json", "{not json")
    for name, expected in [
        ("notes.md", 404),
        ("weird.json", 404),
        ("broken.json", 422),
    ]:
        response = await client.get(f"/api/reports/{name}")
        assert response.status_code == expected, name
        assert "artifacts" not in response.text.lower().replace(
            "artifact", ""
        )


@pytest.mark.parametrize(
    "name",
    ["..", "../secret.json", "..%2Fsecret.json", "%2e%2e%2fsecret.json",
     "sub%2F..%2Fsecret.json", "%2Fetc%2Fpasswd"],
)
async def test_traversal_attempts_are_404(client, artifact_dir: Path, name: str):
    (artifact_dir / "secret.json").write_text(
        json.dumps(_orchestrated_report()), encoding="utf-8"
    )
    response = await client.get(f"/api/reports/{name}")
    assert response.status_code == 404


async def test_symlink_escape_rejected(client, artifact_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_orchestrated_report()), encoding="utf-8")
    link = artifact_dir / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this platform")
    response = await client.get("/api/reports/link.json")
    assert response.status_code == 404
    listing = await client.get("/api/reports")
    assert "link.json" not in [e["name"] for e in listing.json()["entries"]]


async def test_symlink_escape_rejected_without_real_symlink(
    client, artifact_dir: Path, tmp_path: Path, monkeypatch
):
    """Exercise the escaped-path branch without OS symlink privileges."""
    from agentcrash import ui as ui_module

    target = artifact_dir / "evil.json"
    target.write_text(json.dumps(_orchestrated_report()), encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_orchestrated_report()), encoding="utf-8")
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        if self.name == "evil.json":
            return outside
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    assert ui_module.get_artifact_dir() == artifact_dir.resolve()
    with pytest.raises(Exception) as excinfo:
        ui_module.resolve_artifact("evil.json")
    assert getattr(excinfo.value, "status_code", None) == 404
    response = await client.get("/api/reports/evil.json")
    assert response.status_code == 404
    listing = await client.get("/api/reports")
    assert "evil.json" not in [e["name"] for e in listing.json()["entries"]]


def _unified_fixture() -> dict:
    return {
        "note": "offline unified multi-scenario report",
        "scenarios_requested": 1,
        "scenarios_completed": 1,
        "aggregate": {"trials_completed": 1},
        "scenarios": [
            {
                "source": "pay.json",
                "kind": "payment_multi",
                "scenario": "payment_timeout_after_commit",
                "model": "fake-model",
                "trials_requested": 1,
                "trials_completed": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "pass_rate": 1.0,
                "turns_total": 2,
                "successful_charges_total": 1,
                "duplicate_charge_trials": 0,
                "usage": {"status": "missing"},
                "violation_counts": {},
                "baselines": [],
                "trials": [],
                "details": {},
            }
        ],
        "cost_note": "cost_not_calculated (no reliable pricing data)",
    }


async def test_unified_envelope_recognized(client, artifact_dir: Path):
    _write(artifact_dir, "u.json", json.dumps(_unified_fixture()))
    response = await client.get("/api/reports")
    by_name = {e["name"]: e for e in response.json()["entries"]}
    assert by_name["u.json"]["kind"] == "unified"
    assert by_name["u.json"]["supported"] is True
    detail = await client.get("/api/reports/u.json")
    assert detail.status_code == 200
    assert detail.json()["scenarios"][0]["kind"] == "payment_multi"


async def test_unified_requires_envelope_shape(client, artifact_dir: Path):
    # A bare object with a scenarios key is not a unified report.
    _write(artifact_dir, "fake.json", json.dumps({"scenarios": [{"kind": "x"}]}))
    response = await client.get("/api/reports")
    by_name = {e["name"]: e for e in response.json()["entries"]}
    assert by_name["fake.json"]["supported"] is False
    detail = await client.get("/api/reports/fake.json")
    assert detail.status_code == 404


async def test_real_unified_report_recognized_without_modification():
    from agentcrash import ui as ui_module

    real = (
        Path(__file__).resolve().parent.parent
        / "artifacts"
        / "unified-report.json"
    )
    if not real.is_file():
        pytest.skip("historical unified-report.json not present")
    before = real.stat().st_mtime_ns
    kind, supported = ui_module._classify_artifact(real, real.stat().st_size)
    assert kind == "unified"
    assert supported is True
    assert real.stat().st_mtime_ns == before


async def test_size_guard_boundary(client, artifact_dir: Path, monkeypatch):
    from agentcrash import ui as ui_module

    assert ui_module.MAX_CLASSIFY_BYTES == 10_000_000
    small = json.dumps(_orchestrated_report())
    monkeypatch.setattr(ui_module, "MAX_CLASSIFY_BYTES", len(small) + 10)
    _write(artifact_dir, "small.json", small)
    big = small + " " * 50
    assert len(big) > len(small) + 10
    _write(artifact_dir, "big.json", big)

    response = await client.get("/api/reports")
    by_name = {e["name"]: e for e in response.json()["entries"]}
    assert by_name["small.json"]["supported"] is True
    assert by_name["big.json"]["kind"] == "unknown"
    assert by_name["big.json"]["supported"] is False
    detail = await client.get("/api/reports/big.json")
    assert detail.status_code == 404


async def test_strategy_labels_belong_to_supported_values(client):
    response = await client.get("/api/scenarios")
    for scenario in response.json()["scenarios"]:
        assert scenario["strategies"]["unsafe"] in scenario["strategy_values"]
        assert scenario["strategies"]["safe"] in scenario["strategy_values"]
        assert (
            scenario["strategies"]["unsafe"] != scenario["strategies"]["safe"]
        )


async def test_directories_are_not_served(client, artifact_dir: Path):
    (artifact_dir / "subdir").mkdir()
    response = await client.get("/api/reports/subdir")
    assert response.status_code == 404


async def test_simulator_routes_unchanged(client):
    inventory = await client.post(
        "/inventory", json={"item": "widget", "quantity_on_hand": 10}
    )
    assert inventory.status_code == 201
    order = await client.post(
        "/orders", json={"item": "widget", "quantity": 2, "unit_price": "12.50"}
    )
    assert order.status_code == 201
    evaluation = await client.get("/evaluation")
    assert evaluation.status_code == 200
    assert evaluation.json()["valid"] is True


async def test_docs_and_openapi_accessible(client):
    docs = await client.get("/docs")
    assert docs.status_code == 200
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/health" in paths
    assert "/api/reports" in paths
    assert "/inventory" in paths
