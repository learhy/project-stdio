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


# ── Bundle 6.3: QA worker enhancement tests ────────────────────────────────


class TestCriterionScoreModel:
    def test_criterion_score_defaults(self):
        from studio.orchestrator.artifacts import CriterionScore
        c = CriterionScore()
        assert c.criterion == ""
        assert c.score == 0.0
        assert c.pass_fail is False

    def test_criterion_score_passing(self):
        from studio.orchestrator.artifacts import CriterionScore
        c = CriterionScore(criterion="Tests pass", score=0.9, evidence="602 passed", pass_fail=True)
        assert c.pass_fail is True
        assert c.score >= 0.7

    def test_criterion_score_failing(self):
        from studio.orchestrator.artifacts import CriterionScore
        c = CriterionScore(criterion="Coverage >= 80%", score=0.3, evidence="72% coverage", pass_fail=False)
        assert c.pass_fail is False
        assert c.score < 0.7


class TestDeepVerification:
    @pytest.mark.asyncio
    async def test_run_deep_verification_without_strategy(self):
        from studio.workers.qa import _run_deep_verification
        result = await _run_deep_verification("/tmp", None)
        assert "passed" in result

    @pytest.mark.asyncio
    async def test_run_deep_verification_with_library_strategy(self):
        from studio.workers.qa import _run_deep_verification
        with patch("studio.workers.verification.VerificationRunner.run") as mock_run:
            mock_run.return_value = type("obj", (), {"passed": True, "output": "All good", "failures": []})()
            mock_run.return_value.failures = []
            result = await _run_deep_verification("/tmp", {"type": "library", "test_command": "pytest"})
            assert result["passed"] is True


class TestDeveloperAttemptAnalysis:
    @pytest.mark.asyncio
    async def test_analyze_developer_attempts_multi(self):
        from studio.workers.qa import _analyze_developer_attempts
        with patch("studio.workers.qa._call_llm") as mock_llm:
            mock_llm.return_value = {"summary": "Spec was ambiguous about dependencies"}
            result = await _analyze_developer_attempts(3, "http://test", "test-model")
            assert "ambiguous" in result

    @pytest.mark.asyncio
    async def test_analyze_developer_attempts_llm_failure(self):
        from studio.workers.qa import _analyze_developer_attempts
        with patch("studio.workers.qa._call_llm", side_effect=Exception("API down")):
            result = await _analyze_developer_attempts(2, "http://test", "test-model")
            assert "2 attempts" in result


class TestQaSelfFixLoop:
    @pytest.mark.asyncio
    async def test_qa_self_fix_passes_first_attempt(self):
        from studio.workers.qa import _qa_self_fix_loop
        from studio.orchestrator.artifacts import CriterionScore

        with patch("studio.workers.qa._call_llm") as mock_llm:
            mock_llm.return_value = {
                "overall_outcome": "passed",
                "criteria_results": [
                    {"criterion": "Tests pass", "passed": True, "evidence": "OK"},
                    {"criterion": "Coverage >= 80%", "passed": True, "evidence": "85%"},
                ],
                "summary": "All criteria met",
            }
            with patch("studio.workers.qa._escalate_qa_to_pm") as mock_escalate:
                report, scores = await _qa_self_fix_loop(
                    "prompt", ["Tests pass", "Coverage >= 80%"],
                    "/tmp", None, "http://test", "test-model",
                )
            assert report["overall_outcome"] == "passed"
            assert len(scores) == 2
            assert all(s.pass_fail for s in scores)
            mock_escalate.assert_not_called()

    @pytest.mark.asyncio
    async def test_qa_self_fix_retries_on_failure(self):
        from studio.workers.qa import _qa_self_fix_loop
        from studio.orchestrator.artifacts import CriterionScore

        with patch("studio.workers.qa._call_llm") as mock_llm:
            # First call: one criterion fails
            # Second call: all pass
            mock_llm.side_effect = [
                {
                    "overall_outcome": "partial",
                    "criteria_results": [
                        {"criterion": "Tests pass", "passed": True, "evidence": "OK"},
                        {"criterion": "Coverage >= 80%", "passed": False, "evidence": "72%"},
                    ],
                    "summary": "Coverage too low",
                },
                {
                    "overall_outcome": "passed",
                    "criteria_results": [
                        {"criterion": "Tests pass", "passed": True, "evidence": "OK"},
                        {"criterion": "Coverage >= 80%", "passed": True, "evidence": "Fixed — 82%"},
                    ],
                    "summary": "All criteria met after fix",
                },
            ]
            with patch("studio.workers.qa._build_qa_fix_prompt") as mock_build:
                mock_build.return_value = "fixed prompt"
                with patch("studio.workers.qa._escalate_qa_to_pm") as mock_escalate:
                    report, scores = await _qa_self_fix_loop(
                        "prompt", ["Tests pass", "Coverage >= 80%"],
                        "/tmp", None, "http://test", "test-model",
                    )
            assert report["overall_outcome"] == "passed"
            assert mock_llm.call_count == 2
            mock_escalate.assert_not_called()

    @pytest.mark.asyncio
    async def test_qa_escalates_after_two_attempts(self):
        from studio.workers.qa import _qa_self_fix_loop

        with patch("studio.workers.qa._call_llm") as mock_llm:
            # Both attempts fail
            mock_llm.return_value = {
                "overall_outcome": "failed",
                "criteria_results": [
                    {"criterion": "Tests pass", "passed": False, "evidence": "Tests broken"},
                ],
                "summary": "Unfixable",
            }
            with patch("studio.workers.qa._build_qa_fix_prompt") as mock_build:
                mock_build.return_value = "fixed prompt"
                with patch("studio.workers.qa._escalate_qa_to_pm") as mock_escalate:
                    report, scores = await _qa_self_fix_loop(
                        "prompt", ["Tests pass"],
                        "/tmp", None, "http://test", "test-model",
                    )
            assert report["overall_outcome"] == "failed"
            assert mock_llm.call_count == 2
            mock_escalate.assert_called()
