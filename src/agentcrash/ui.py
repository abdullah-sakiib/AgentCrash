"""Read-only UI API for browsing experiment artifacts (M13 Phase A).

Serves existing JSON reports from the artifact directory. No experiment
execution, no writes, no credentials, no model calls.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agentcrash.experiments.compare_order_fulfillment import (
    StrategyName as FulfillmentStrategies,
)
from agentcrash.experiments.compare_order_intent import (
    StrategyName as IntentStrategies,
)
from agentcrash.experiments.compare_payment_recovery import (
    StrategyName as PaymentStrategies,
)
from agentcrash.experiments.run_experiment import (
    FULFILLMENT_KEY,
    FULFILLMENT_SCENARIO,
    INTENT_KEY,
    INTENT_TRIAL_SCENARIO,
    PAYMENT_KEY,
    PAYMENT_SCENARIO,
)
from agentcrash.experiments.scorecard import DEFAULT_FAULTS, SCENARIO_ALIASES
from agentcrash.experiments.unified_report import (
    UnifiedReportError,
    _detect_kind,
)

router = APIRouter(prefix="/api", tags=["ui"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# Listing and retrieval never parse more than this many bytes per file.
# Oversized artifacts stay listed (and retrievable only as unsupported).
MAX_CLASSIFY_BYTES = 10_000_000


def get_artifact_dir() -> Path:
    """Configured artifact directory (env override is for tests)."""
    override = os.environ.get("AGENTCRASH_ARTIFACTS_DIR")
    if override:
        return Path(override).resolve()
    return (Path(__file__).resolve().parent.parent.parent / "artifacts").resolve()


def _not_found() -> HTTPException:
    # Identical response for missing, escaped, and non-file paths so the
    # API never distinguishes them (and never leaks absolute paths).
    return HTTPException(status_code=404, detail="Artifact not found.")


def resolve_artifact(name: str) -> Path:
    """Resolve an artifact name inside the artifact directory.

    Rejects traversal, absolute paths, symlink escapes, directories, and
    non-regular files with a uniform 404.
    """
    if not name or name in (".", ".."):
        raise _not_found()
    base = get_artifact_dir()
    try:
        candidate = (base / name).resolve()
    except (OSError, ValueError):
        raise _not_found() from None
    if not candidate.is_relative_to(base):
        raise _not_found()
    try:
        if not candidate.is_file():
            raise _not_found()
    except OSError:
        raise _not_found() from None
    return candidate


def _reverse_aliases() -> dict[str, list[str]]:
    reversed_map: dict[str, list[str]] = {}
    for alias, canonical in SCENARIO_ALIASES.items():
        reversed_map.setdefault(canonical, []).append(alias)
    return reversed_map


def scenario_catalog() -> list[dict[str, Any]]:
    """Scenario catalog built from existing project constants only."""
    aliases = _reverse_aliases()
    return [
        {
            "key": PAYMENT_KEY,
            "canonical_name": PAYMENT_SCENARIO,
            "aliases": aliases.get(PAYMENT_SCENARIO, []),
            "fault": dict(DEFAULT_FAULTS[PAYMENT_SCENARIO]),
            "strategies": {
                "unsafe": "unsafe_retry",
                "safe": "status_check",
            },
            "strategy_values": sorted(get_args(PaymentStrategies)),
            "invariants": ["multiple_successful_charges_for_order"],
            "description": (
                "A payment commits in persisted state but the API returns "
                "HTTP 504. The unsafe strategy retries with a new idempotency "
                "key and creates a duplicate charge; the safe strategy checks "
                "payment status first and leaves exactly one charge."
            ),
        },
        {
            "key": FULFILLMENT_KEY,
            "canonical_name": FULFILLMENT_SCENARIO,
            "aliases": aliases.get(FULFILLMENT_SCENARIO, []),
            "fault": dict(DEFAULT_FAULTS[FULFILLMENT_SCENARIO]),
            "strategies": {
                "unsafe": "unsafe_fulfill",
                "safe": "safe_pay_then_fulfill",
            },
            "strategy_values": sorted(get_args(FulfillmentStrategies)),
            "invariants": ["fulfilled_without_payment"],
            "description": (
                "An order is fulfilled without a successful payment. The "
                "unsafe strategy fulfills the unpaid order; the safe strategy "
                "charges first and only then fulfills."
            ),
        },
        {
            "key": INTENT_KEY,
            "canonical_name": INTENT_TRIAL_SCENARIO,
            "aliases": aliases.get(INTENT_TRIAL_SCENARIO, []),
            "fault": dict(DEFAULT_FAULTS[INTENT_TRIAL_SCENARIO]),
            "strategies": {
                "unsafe": "unsafe_duplicate",
                "safe": "safe_deduplicated",
            },
            "strategy_values": sorted(get_args(IntentStrategies)),
            "invariants": ["duplicate_orders_for_intent"],
            "description": (
                "The same customer intent is submitted twice. The unsafe "
                "strategy creates two orders and deducts stock twice; the "
                "safe strategy looks the intent up first and creates exactly "
                "one order."
            ),
        },
    ]


def _is_unified_envelope(data: Any) -> bool:
    """Recognize the actual unified-report envelope from build_unified_report.

    Requires the envelope shape (aggregate summary plus kind-labeled
    scenario entries), not merely any JSON object with a "scenarios" key.
    """
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("aggregate"), dict):
        return False
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return False
    first = scenarios[0]
    return isinstance(first, dict) and isinstance(first.get("kind"), str)


def _classify_artifact(path: Path, size_bytes: int) -> tuple[str, bool]:
    """Return (kind, supported) for an artifact file; never raises."""
    if size_bytes > MAX_CLASSIFY_BYTES:
        return "unknown", False
    if path.suffix.lower() == ".md":
        return "markdown", False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return "unknown", False
    if not isinstance(data, dict):
        return "unknown", False
    if _is_unified_envelope(data):
        return "unified", True
    try:
        kind = _detect_kind(data, path.name)
    except UnifiedReportError:
        return "unknown", False
    return kind, True


def _entry_for(path: Path) -> dict[str, Any] | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    base = get_artifact_dir()
    if not resolved.is_relative_to(base):
        return None
    try:
        if not resolved.is_file():
            return None
        stat = resolved.stat()
    except OSError:
        return None
    kind, supported = _classify_artifact(resolved, stat.st_size)
    try:
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        mtime = None
    return {
        "name": path.name,
        "size_bytes": stat.st_size,
        "mtime": mtime,
        "kind": kind,
        "supported": supported,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    base = get_artifact_dir()
    try:
        readable = base.is_dir()
    except OSError:
        readable = False
    return {"status": "ok", "artifacts_readable": readable}


@router.get("/scenarios")
def list_scenarios() -> dict[str, Any]:
    return {"scenarios": scenario_catalog()}


@router.get("/reports")
def list_reports(limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
    if limit < 1 or limit > MAX_LIMIT or offset < 0:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be 1..{MAX_LIMIT} and offset >= 0.",
        )
    base = get_artifact_dir()
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name) if base.is_dir() else []
    except OSError:
        children = []
    for child in children:
        entry = _entry_for(child)
        if entry is not None:
            entries.append(entry)
    entries.sort(
        key=lambda e: (e["mtime"] or "", e["name"]), reverse=True
    )
    return {
        "total": len(entries),
        "limit": limit,
        "offset": offset,
        "entries": entries[offset : offset + limit],
    }


def _read_report_document(name: str) -> Any:
    path = resolve_artifact(name)
    if path.suffix.lower() == ".md":
        raise HTTPException(
            status_code=404, detail=f"Artifact '{name}' is not a supported report."
        )
    try:
        size = path.stat().st_size
    except OSError:
        raise _not_found() from None
    if size > MAX_CLASSIFY_BYTES:
        raise HTTPException(
            status_code=404, detail=f"Artifact '{name}' is not a supported report."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise _not_found() from None
    try:
        data = json.loads(text)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"Artifact '{name}' is not valid JSON."
        ) from None
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=404, detail=f"Artifact '{name}' is not a supported report."
        )
    if _is_unified_envelope(data):
        return data
    try:
        _detect_kind(data, name)
    except UnifiedReportError:
        raise HTTPException(
            status_code=404, detail=f"Artifact '{name}' is not a supported report."
        ) from None
    return data


@router.get("/reports/{name}")
def get_report(name: str) -> Any:
    return _read_report_document(name)


@router.get("/reports/{name}/diagnostics")
def get_diagnostics(
    name: str,
    scenario: str | None = None,
    trial_id: str | None = None,
    trial_index: int | None = None,
) -> Any:
    """Compute per-trial diagnostics in memory; never persists or rewrites."""
    from agentcrash.experiments.diagnostics import (
        DIAGNOSTICS_VERSION,
        build_report_diagnostics,
        iter_report_trials,
    )

    data = _read_report_document(name)
    entries = iter_report_trials(data if isinstance(data, dict) else {})
    diagnostics = build_report_diagnostics(data if isinstance(data, dict) else {})

    def _matches(entry: dict[str, Any], position: int) -> bool:
        trial = entry["trial"] if isinstance(entry.get("trial"), dict) else {}
        entry_scenario = entry.get("scenario")
        if scenario is not None and entry_scenario != scenario:
            return False
        if trial_id is not None:
            candidates = {trial.get("trial_id"), trial.get("strategy_name")}
            if trial_id not in {c for c in candidates if c is not None}:
                return False
            return True
        if trial_index is not None:
            return trial.get("trial_index") == trial_index
        return True

    has_filter = scenario is not None or trial_id is not None or trial_index is not None
    if not has_filter:
        return {
            "diagnostics_version": DIAGNOSTICS_VERSION,
            "source": name,
            "diagnostics": diagnostics,
        }
    matched = [
        (position, entry, diagnostic)
        for position, (entry, diagnostic) in enumerate(zip(entries, diagnostics))
        if _matches(entry, position)
    ]
    if not matched:
        raise HTTPException(status_code=404, detail=f"No matching trial in '{name}'.")
    # Trial-index fallback applies only when trial_id is null.
    if trial_id is None and trial_index is not None and len(matched) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Multiple trials match trial_index {trial_index} in '{name}'.",
                "candidates": [
                    {
                        "scenario": entry.get("scenario"),
                        "trial_id": entry["trial"].get("trial_id", entry["trial"].get("strategy_name")),
                        "trial_index": entry["trial"].get("trial_index"),
                    }
                    for _, entry, _ in matched
                ],
            },
        )
    if trial_id is not None and len(matched) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Multiple trials match trial_id '{trial_id}' in '{name}'.",
                "candidates": [
                    {
                        "scenario": entry.get("scenario"),
                        "trial_id": entry["trial"].get("trial_id", entry["trial"].get("strategy_name")),
                        "trial_index": entry["trial"].get("trial_index"),
                    }
                    for _, entry, _ in matched
                ],
            },
        )
    _, _, diagnostic = matched[0]
    return diagnostic


@router.get("/reports/{name}/scorecard")
def get_scorecard(name: str, strategy_id: str | None = None) -> Any:
    """Score one artifact with the existing scorecard implementation.

    Metrics are never recalculated here; the M11 document is returned
    verbatim (or a sanitized 422 when the artifact is not scoreable).
    """
    from agentcrash.experiments.scorecard import build_scorecard_document

    data = _read_report_document(name)
    try:
        return build_scorecard_document(
            data, strategy_id=strategy_id, source=name
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(
            status_code=422, detail=f"Artifact '{name}' cannot be scored."
        ) from None


@router.get("/comparisons")
def get_comparison(baseline: str, candidate: str) -> Any:
    """Compare two artifact scorecards with the existing M12 implementation.

    Compatibility checks, metric calculations, and regression logic all
    come from compare_scorecard_documents() verbatim. Incompatible inputs
    return a normal 200 response with compatible:false, never a ranking.
    """
    from agentcrash.experiments.scorecard import (
        build_scorecard_document,
        compare_scorecard_documents,
    )

    baseline_doc = _scorable_artifact(baseline, "baseline")
    candidate_doc = _scorable_artifact(candidate, "candidate")
    try:
        baseline_scored = build_scorecard_document(
            baseline_doc, source=baseline
        )
        candidate_scored = build_scorecard_document(
            candidate_doc, source=candidate
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(
            status_code=422,
            detail="One of the artifacts cannot be scored.",
        ) from None
    try:
        return compare_scorecard_documents(baseline_scored, candidate_scored)
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(
            status_code=422, detail="Scorecards cannot be compared."
        ) from None


def _scorable_artifact(name: str, role: str) -> Any:
    """Resolve and parse a comparison input with role-attributed errors."""
    try:
        return _read_report_document(name)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"{role.capitalize()} artifact '{name}' was not found "
                "or is not a supported report.",
            ) from None
        raise


class SeriesRequest(BaseModel):
    """Explicit, ordered series membership; nothing is inferred or scanned."""

    baseline: str
    members: list[str]
    strategy_id: str | None = None
    strategy_ids: dict[str, str] | None = None


@router.post("/series")
def get_series(body: SeriesRequest) -> Any:
    """Build a descriptive reliability series over named artifacts.

    Membership is explicit: the baseline plus an ordered member list.
    Strategy identity is user-attested via ``strategy_id`` (all members)
    with per-artifact ``strategy_ids`` overrides; unlabeled or mismatched
    runs are excluded as unconfirmed, never silently grouped. The result is
    computed on demand and never persisted.
    """
    from agentcrash.experiments.scorecard import (
        MAX_SERIES_MEMBERS,
        build_scorecard_document,
        build_series_trend,
    )

    if not body.members:
        raise HTTPException(
            status_code=400,
            detail="members must be a non-empty list of artifact names.",
        )
    if len(body.members) > MAX_SERIES_MEMBERS:
        raise HTTPException(
            status_code=400,
            detail=f"members must contain at most {MAX_SERIES_MEMBERS} entries.",
        )
    labels = dict(body.strategy_ids or {})

    def label_for(name: str) -> str | None:
        override = labels.get(name)
        if isinstance(override, str) and override.strip():
            return override.strip()
        if isinstance(body.strategy_id, str) and body.strategy_id.strip():
            return body.strategy_id.strip()
        return None

    def scored(name: str, role: str) -> Any:
        try:
            report = _scorable_artifact(name, role)
        except HTTPException as exc:
            if exc.status_code == 404 and role.startswith("member"):
                raise HTTPException(
                    status_code=404,
                    detail=f"Member artifact '{name}' was not found "
                    "or is not a supported report.",
                ) from None
            raise
        try:
            return build_scorecard_document(
                report, strategy_id=label_for(name), source=name
            )
        except (ValueError, TypeError, AttributeError, KeyError):
            raise HTTPException(
                status_code=422,
                detail=f"Artifact '{name}' cannot be scored.",
            ) from None

    baseline_doc = scored(body.baseline, "baseline")
    member_docs = [scored(name, "member") for name in body.members]
    try:
        return build_series_trend(baseline_doc, member_docs)
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(
            status_code=422, detail="Series cannot be built."
        ) from None
