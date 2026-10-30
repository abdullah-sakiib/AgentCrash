"""Regression tests for demo-judge.ps1 report selection.

A directory holding two demo reports plus a *newer* scorecard sidecar must
still resolve to the newest report — never the sidecar. The test
dot-sources the real script (main body guarded) and calls its actual
Select-NewestDemoReport helper. No historical artifacts are touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "demo-judge.ps1"


def _run_selection(artifacts_dir: Path, extra_args: str = "") -> str | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available on this platform")
    command = (
        f". '{SCRIPT}'; "
        f"$selected = Select-NewestDemoReport -ArtifactsDir '{artifacts_dir}'"
        f"{extra_args}; "
        "if ($null -eq $selected) { 'NONE' } else { $selected.Name }"
    )
    proc = subprocess.run(
        [powershell, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, f"helper produced no output: {proc.stderr}"
    result = lines[-1]
    return None if result == "NONE" else result


def _touch(path: Path, epoch: int) -> None:
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (epoch, epoch))


def test_newest_report_wins_over_newer_sidecar(tmp_path: Path):
    arts = tmp_path / "arts"
    arts.mkdir()
    _touch(arts / "demo-hackathon-offline-20261030-120000.json", 1_700_000_000)
    _touch(arts / "demo-hackathon-offline-20261030-130000.json", 1_700_000_100)
    # Sidecar is the newest file on disk but must never be selected.
    _touch(
        arts / "demo-hackathon-offline-20261030-140000.scorecard.json",
        1_700_000_200,
    )
    assert _run_selection(arts) == "demo-hackathon-offline-20261030-130000.json"


def test_exclusion_list_skips_just_created_report(tmp_path: Path):
    arts = tmp_path / "arts"
    arts.mkdir()
    _touch(arts / "demo-hackathon-offline-20261030-120000.json", 1_700_000_000)
    newest = arts / "demo-hackathon-offline-20261030-130000.json"
    _touch(newest, 1_700_000_100)
    _touch(
        arts / "demo-hackathon-offline-20261030-140000.scorecard.json",
        1_700_000_200,
    )
    selected = _run_selection(
        arts, f" -ExcludeFullNames @('{newest}')"
    )
    assert selected == "demo-hackathon-offline-20261030-120000.json"


def test_empty_directory_selects_nothing(tmp_path: Path):
    arts = tmp_path / "arts"
    arts.mkdir()
    assert _run_selection(arts) is None
