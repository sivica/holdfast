from __future__ import annotations

from hlguard.evalrunner import compare_baseline, run_suite


def test_suite_matches_baseline() -> None:
    report = run_suite()
    assert report["policy_compliance"] == 1.0
    assert compare_baseline(report) is None
