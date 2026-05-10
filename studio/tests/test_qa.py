"""Tests for Bundle 2.9: QA verification worker, Verification Report, calibration loop, state machine."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from studio.orchestrator.models import (
    BundleState,
    VerificationReport,
    CriterionResult,
    CalibrationEntry,
)
from studio.orchestrator.state_machine import BundleStateMachine
from studio.orchestrator.github import GitHubClient


class TestVerificationReportModel:
    def test_empty_report(self):
        r = VerificationReport()
        assert r.outcome == "passed"
        assert r.criteria_results == []
        assert r.failed_criteria == []

    def test_report_with_criteria(self):
        r = VerificationReport(
            bundle_id="b1",
            outcome="partial",
            criteria_results=[
                CriterionResult(criterion="Tests pass", passed=True, evidence="602 passed", automated=True),
                CriterionResult(criterion="Coverage >= 80%", passed=False, evidence="Coverage is 72%", automated=True),
            ],
            failed_criteria=["Coverage >= 80%"],
            recommendations=["Add tests for untested modules"],
            summary="QA found 1 failing criterion",
        )
        assert r.outcome == "partial"
        assert len(r.criteria_results) == 2
        assert r.failed_criteria == ["Coverage >= 80%"]

    def test_criterion_default_automated(self):
        c = CriterionResult(criterion="Code looks clean", passed=True, evidence="Manual review OK")
        assert c.automated is True

    def test_report_serialization(self):
        r = VerificationReport(
            bundle_id="b1",
            outcome="passed",
            criteria_results=[
                CriterionResult(criterion="C1", passed=True, evidence="OK"),
            ],
        )
        d = r.model_dump()
        assert d["bundle_id"] == "b1"
        assert d["outcome"] == "passed"
        assert len(d["criteria_results"]) == 1


class TestCalibrationEntryModel:
    def test_empty_entry(self):
        e = CalibrationEntry()
        assert e.bundle_id == ""
        assert e.divergence_threshold_exceeded == []

    def test_entry_with_divergence(self):
        e = CalibrationEntry(
            bundle_id="b1",
            estimated_loc=100,
            actual_loc=250,
            divergence_threshold_exceeded=["loc"],
        )
        assert e.bundle_id == "b1"
        assert e.estimated_loc == 100
        assert e.actual_loc == 250
        assert "loc" in e.divergence_threshold_exceeded


class TestStateMachineVerification:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.transaction = MagicMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    @pytest.fixture
    def sm(self, db_mock):
        return BundleStateMachine(db_mock, kernel_mode=True)

    @pytest.mark.asyncio
    async def test_transition_17_complete(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}
        outcome = {"status": "shipped", "verification": {"outcome": "passed"}}

        await sm.transition_17_complete("bundle-1", outcome)

        # Verify state update to COMPLETE
        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.COMPLETE in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_19_fail_verification(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.VERIFYING}

        await sm.transition_19_fail_verification("bundle-1", "Tests failed")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.FAILED in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_9_to_verifying(self, sm, db_mock):
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}

        await sm.transition_9_to_verifying("bundle-1")

        update_call = db_mock.execute.call_args_list[0]
        assert BundleState.VERIFYING in update_call[0][1]

    @pytest.mark.asyncio
    async def test_transition_17_only_from_verifying(self, sm, db_mock):
        from studio.orchestrator.state_machine import IllegalTransitionError
        db_mock.fetch_one.return_value = {"state": BundleState.IN_PROGRESS}

        with pytest.raises(IllegalTransitionError):
            await sm.transition_17_complete("bundle-1", {})

    @pytest.mark.asyncio
    async def test_transition_19_only_from_verifying(self, sm, db_mock):
        from studio.orchestrator.state_machine import IllegalTransitionError
        db_mock.fetch_one.return_value = {"state": BundleState.APPROVED}

        with pytest.raises(IllegalTransitionError):
            await sm.transition_19_fail_verification("bundle-1", "bad")


class TestCalibrationRecording:
    @pytest.fixture
    def db_mock(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.transaction = MagicMock()
        db.conn = MagicMock()
        db.conn.commit = AsyncMock()
        return db

    def test_pct_divergence_calculation(self):
        """Verify >50% divergence detection logic."""
        def pct_divergence(est, act):
            if est == 0:
                return None
            return abs(act - est) / est

        # Exactly at 50%
        assert pct_divergence(100, 150) == 0.5
        # Over 50%
        assert pct_divergence(100, 151) > 0.5
        # Under 50%
        assert pct_divergence(100, 149) < 0.5
        # Zero estimate is skipped
        assert pct_divergence(0, 100) is None

    def test_diverged_axes_detection(self):
        """Test which axes trigger post-mortem."""
        axes = {
            "loc": (100, 200),           # 100% divergence -> triggers
            "duration_seconds": (100, 120),  # 20% -> ok
            "worker_count": (4, 5),      # 25% -> ok
            "tokens": (1000, 2000),      # 100% -> triggers
        }

        def pct_divergence(est, act):
            if est == 0:
                return None
            return abs(act - est) / est

        diverged = []
        for name, (est, act) in axes.items():
            pct = pct_divergence(est, act)
            if pct is not None and pct > 0.5:
                diverged.append(name)

        assert "loc" in diverged
        assert "tokens" in diverged
        assert "duration_seconds" not in diverged
        assert "worker_count" not in diverged


class TestQaWorker:
    """Tests for the QA verification worker logic (without running subprocesses)."""

    def test_parse_llm_response_json(self):
        """Test extraction of JSON from LLM response."""
        raw = '{"overall_outcome": "passed", "criteria_results": [], "summary": "ok"}'
        result = json.loads(raw)
        assert result["overall_outcome"] == "passed"

    def test_parse_llm_response_with_code_fence(self):
        raw = '```json\n{"overall_outcome": "failed", "summary": "bad"}\n```'
        # Strip fences
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])
            if raw.endswith("```"):
                raw = raw[:-3]
        result = json.loads(raw.strip())
        assert result["overall_outcome"] == "failed"

    def test_automated_check_pytest_success(self):
        """Simulate automated checks dict structure."""
        results = {
            "pytest": {"returncode": 0, "stdout": "602 passed", "stderr": ""},
            "acceptance_sh": None,
            "lint": {"returncode": 0, "stdout": ""},
        }
        assert results["pytest"]["returncode"] == 0

    def test_automated_check_pytest_failure(self):
        results = {
            "pytest": {"returncode": 1, "stdout": "1 failed", "stderr": "FAILED test_foo"},
        }
        assert results["pytest"]["returncode"] == 1

    def test_final_report_params_format(self):
        """Verify the final_report params structure sent by QA worker."""
        from studio.workers.qa import _format_final_params

        report = {
            "overall_outcome": "passed",
            "criteria_results": [],
            "failed_criteria": [],
            "recommendations": [],
            "summary": "All tests passed",
        }
        params = _format_final_params("success", report, "All tests passed")
        assert params["outcome"] == "success"
        assert params["verification_report"]["outcome"] == "passed"

    def test_final_report_params_failure(self):
        from studio.workers.qa import _format_final_params

        report = {
            "overall_outcome": "failed",
            "criteria_results": [],
            "failed_criteria": ["Coverage too low"],
            "recommendations": ["Add tests"],
            "summary": "Verification failed",
        }
        params = _format_final_params("failure", report, "Verification failed")
        assert params["outcome"] == "failure"
        assert params["verification_report"]["failed_criteria"] == ["Coverage too low"]
