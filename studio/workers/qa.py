"""Post-execution QA verification worker: validates shipped bundle against Verification Plan.

Runs after all DAG workers complete (bundle in VERIFYING state). Reads the
Verification Plan artifact, runs automated checks, performs an LLM pass, produces
a Verification Report artifact, and calls worker.final_report with pass/fail.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any

from .client import RpcClient, get_orchestrator_addr_display

# ── Configuration from environment ────────────────────────────────────────────

_TOKEN = os.environ.get("STUDIO_WORKER_TOKEN", "")
_WORKER_ID = os.environ.get("STUDIO_WORKER_ID", "qa-verification-unknown")
_BUNDLE_ID = os.environ.get("STUDIO_BUNDLE_ID", "unknown")
_TASK_SPEC_RAW = os.environ.get("STUDIO_TASK_SPEC", "{}")
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api")
_BUNDLE_BRANCH = os.environ.get("STUDIO_BUNDLE_BRANCH", "")
_REPO_PATH = os.environ.get("STUDIO_REPO_PATH", "")

# ── System prompt ─────────────────────────────────────────────────────────────

_QA_SYSTEM_PROMPT = """You are the Studio post-execution QA verification agent. Your job is to validate a shipped bundle against its Verification Plan. The DAG workers have completed their tasks. You inspect the actual output and judge whether each acceptance criterion is met.

## Input

You will receive:
1. The Verification Plan (acceptance criteria, test surface, pre-merge gates, post-ship verification metrics, rollback plan)
2. Automated check results (pytest output, coverage percentages, lint results, acceptance.sh output)
3. The code diff (git diff of the bundle branch)

## Your task

For each acceptance criterion in the Verification Plan, you must:
- State whether it PASSED or FAILED
- Provide specific evidence from the automated checks or code diff
- For FAILED criteria, explain what went wrong and recommend what needs to change

For criteria that automated checks cannot verify (design quality, architectural fit, UX coherence), use the code diff and your own judgment.

## Output format

Respond with a single JSON object matching this schema:

```json
{
  "criteria_results": [
    {
      "criterion": "<the acceptance criterion text>",
      "passed": true,
      "evidence": "<specific evidence: test output, diff snippet, observation>",
      "automated": false
    }
  ],
  "overall_outcome": "passed|failed|partial",
  "failed_criteria": ["<text of each failed criterion>"],
  "recommendations": ["<what to fix for each failure>"],
  "summary": "<2-3 sentence overall QA assessment>"
}
```

## Hard rules
- Be honest. If a criterion is not testable from the diff, mark it failed and explain why.
- Do not pass a criterion just because "it looks fine." Demand evidence.
- If any criterion fails, the overall_outcome must be "failed" or "partial".
- Partial means: some criteria passed, failures are non-blocking or cosmetic. Failed means: one or more criteria fail in a blocking way.
"""


# ── Main ───────────────────────────────────────────────────────────────────────


def _run_automated_checks(repo_path: str) -> dict:
    results: dict[str, Any] = {"pytest": None, "coverage": None, "lint": None, "acceptance_sh": None}

    # pytest
    try:
        proc = subprocess.run(
            ["pytest", "studio/tests/", "-q", "--tb=short"],
            capture_output=True, text=True, timeout=120, cwd=repo_path,
        )
        results["pytest"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-2000:],
            "stderr": proc.stderr[-500:],
        }
    except Exception as exc:
        results["pytest"] = {"error": str(exc)}

    # acceptance.sh
    acc_path = os.path.join(repo_path, "studio/tests/acceptance.sh")
    if os.path.exists(acc_path):
        try:
            proc = subprocess.run(
                ["bash", acc_path],
                capture_output=True, text=True, timeout=120, cwd=repo_path,
            )
            results["acceptance_sh"] = {
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-500:],
            }
        except Exception as exc:
            results["acceptance_sh"] = {"error": str(exc)}

    # lint (flake8 or ruff if available)
    try:
        proc = subprocess.run(
            ["ruff", "check", "studio/", "--output-format=concise"],
            capture_output=True, text=True, timeout=60, cwd=repo_path,
        )
    except FileNotFoundError:
        try:
            proc = subprocess.run(
                ["flake8", "studio/", "--max-line-length=120"],
                capture_output=True, text=True, timeout=60, cwd=repo_path,
            )
        except Exception:
            results["lint"] = {"skipped": "ruff/flake8 not available"}
            proc = None
    if proc is not None:
        results["lint"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1000:],
        }

    return results


def _get_code_diff(repo_path: str, bundle_branch: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", f"origin/main...{bundle_branch}"],
            capture_output=True, text=True, timeout=30, cwd=repo_path,
        )
        return proc.stdout[:15000]
    except Exception:
        return "(unavailable)"


def _format_final_params(
    outcome: str,
    report: dict,
    summary: str,
    criterion_scores: list | None = None,
) -> dict:
    scores = criterion_scores or []
    return {
        "outcome": outcome,
        "summary": summary,
        "verification_report": {
            "bundle_id": _BUNDLE_ID,
            "outcome": report.get("overall_outcome", "failed"),
            "criteria_results": report.get("criteria_results", []),
            "failed_criteria": report.get("failed_criteria", []),
            "recommendations": report.get("recommendations", []),
            "summary": report.get("summary", ""),
            "produced_at": int(time.time()),
            "criterion_scores": _dump_scores(scores),
        },
    }


# ── Deep verification ──────────────────────────────────────────────────────


async def _run_deep_verification(repo_path: str, strategy: dict | None) -> dict:
    from .verification import VerificationRunner
    try:
        runner = VerificationRunner(repo_path, timeout_seconds=60)
        result = await runner.run(strategy)
        return {"passed": result.passed, "output": result.output, "failures": len(result.failures)}
    except Exception as exc:
        return {"passed": False, "output": str(exc), "failures": 1}


# ── Developer attempt analysis ──────────────────────────────────────────────


async def _analyze_developer_attempts(attempts: int, ollama_base_url: str, model: str) -> str:
    prompt = f"""The developer worker needed {attempts} attempts to pass verification.
Review the situation and determine:
1. Was the original spec ambiguous in a way that caused the failures?
2. Did the fixes address the root cause or just the symptoms?
3. Are there likely regression risks from the fixes?

Respond with a concise 2-3 sentence analysis."""
    try:
        result = await _call_llm(prompt, ollama_base_url, model)
        return result.get("summary", result.get("message", ""))
    except Exception:
        return f"Developer needed {attempts} attempts (analysis unavailable)"


# ── QA prompt construction ─────────────────────────────────────────────────


def _build_qa_prompt(
    verification_plan: dict,
    auto_results: dict,
    code_diff: str,
    verify_result: dict,
    attempt_analysis: str,
    bundle_requirements: str,
) -> str:
    parts = [json.dumps({
        "verification_plan": verification_plan,
        "automated_checks": auto_results,
        "deep_verification": verify_result,
        "code_diff": code_diff,
    }, indent=2)]

    if bundle_requirements:
        parts.append(f"\nBundle requirements: {bundle_requirements}")
    if attempt_analysis:
        parts.append(f"\nDeveloper attempt analysis: {attempt_analysis}")

    prompt = "\n".join(parts)
    if len(prompt) > 24000:
        prompt = prompt[:24000]
    return prompt


# ── QA self-fix loop ───────────────────────────────────────────────────────

_MAX_QA_ATTEMPTS = 2


async def _qa_self_fix_loop(
    prompt: str,
    acceptance_criteria: list[str],
    repo_path: str,
    verification_strategy: dict | None,
    ollama_base_url: str,
    model: str,
) -> tuple[dict, list]:
    from studio.orchestrator.artifacts import CriterionScore

    for attempt in range(1, _MAX_QA_ATTEMPTS + 1):
        try:
            llm_result = await _call_llm(prompt, ollama_base_url, model)
        except Exception as exc:
            return {"overall_outcome": "failed", "summary": f"LLM evaluation failed: {exc}",
                    "failed_criteria": [], "recommendations": ["Re-run verification"]}, []

        criterion_scores = _extract_criterion_scores(llm_result, acceptance_criteria)

        failing = [c for c in criterion_scores if not c.pass_fail]
        if not failing:
            return llm_result, criterion_scores

        if attempt < _MAX_QA_ATTEMPTS:
            prompt = await _build_qa_fix_prompt(prompt, criterion_scores, repo_path, ollama_base_url, model)

    # Exhausted — escalate
    await _escalate_qa_to_pm(criterion_scores)
    return {
        "overall_outcome": "failed",
        "summary": f"QA self-fix exhausted after {_MAX_QA_ATTEMPTS} attempts",
        "failed_criteria": [c.criterion for c in criterion_scores if not c.pass_fail],
        "recommendations": ["Escalated to PM for manual review"],
    }, criterion_scores


def _extract_criterion_scores(llm_result: dict, acceptance_criteria: list[str]) -> list:
    from studio.orchestrator.artifacts import CriterionScore

    criteria_results = llm_result.get("criteria_results", [])
    if criteria_results:
        return [
            CriterionScore(
                criterion=cr.get("criterion", acceptance_criteria[i] if i < len(acceptance_criteria) else ""),
                score=0.8 if cr.get("passed") else 0.3,
                evidence=cr.get("evidence", ""),
                pass_fail=cr.get("passed", False),
            )
            for i, cr in enumerate(criteria_results)
        ]

    # Fallback: create from acceptance criteria directly
    return [
        CriterionScore(criterion=c, score=0.5, evidence="Not evaluated", pass_fail=False)
        for c in acceptance_criteria
    ]


async def _build_qa_fix_prompt(
    original_prompt: str,
    criterion_scores: list,
    repo_path: str,
    ollama_base_url: str,
    model: str,
) -> str:
    from studio.orchestrator.artifacts import CriterionScore

    failing_text = "\n".join(
        f"- {c.criterion} (score: {c.score:.1f}): {c.evidence}"
        for c in criterion_scores if not c.pass_fail
    )

    fix_request = f"""Some acceptance criteria failed QA verification:

{failing_text}

Generate specific code fixes for these failures. Respond with the corrected code snippets or instructions for the fix."""

    try:
        fix_result = await _call_llm(fix_request, ollama_base_url, model)
        fixes_applied = await _apply_qa_fixes(repo_path, fix_result)
        return original_prompt + f"\n\nQA fix attempt applied: {fixes_applied}\nRe-evaluate."
    except Exception:
        return original_prompt + "\n\nQA fix attempt failed. Re-evaluate with current state."


async def _apply_qa_fixes(repo_path: str, fix_result: dict) -> str:
    """Apply LLM-generated fixes to files in repo_path. Returns summary."""
    fixes = fix_result.get("fixes", fix_result.get("recommendations", []))
    if not fixes:
        return "No fix instructions generated"

    applied = []
    for fix in fixes[:3]:
        file_path = fix.get("file", "")
        content = fix.get("content", fix.get("patch", ""))
        if file_path and content:
            full_path = os.path.join(repo_path, file_path)
            try:
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w") as f:
                    f.write(content)
                applied.append(file_path)
            except Exception:
                pass

    return f"Applied fixes to: {', '.join(applied)}" if applied else "No files modified"


async def _escalate_qa_to_pm(criterion_scores: list) -> None:
    from studio.orchestrator.artifacts import CriterionScore
    failing = [c for c in criterion_scores if not c.pass_fail]
    if failing:
        print(f"[qa] QA escalation: {len(failing)} failing criteria, "
              f"{len([c for c in criterion_scores if c.pass_fail])} passing",
              file=sys.stderr, flush=True)
        for c in failing:
            print(f"[qa] FAIL: {c.criterion} (score={c.score:.1f}) — {c.evidence}",
                  file=sys.stderr, flush=True)


def _auto_pass_report() -> dict:
    return {
        "overall_outcome": "passed",
        "criteria_results": [],
        "failed_criteria": [],
        "recommendations": [],
        "summary": "Auto-passed QA verification (local/test environment)",
    }


def _dump_scores(scores: list) -> list[dict]:
    return [{"criterion": s.criterion, "score": s.score, "evidence": s.evidence, "pass_fail": s.pass_fail}
            for s in scores]


async def _call_llm(prompt: str, ollama_base_url: str, model: str = "minimax-m2.7:cloud") -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        resp = await client.post(
            f"{ollama_base_url.rstrip('/')}/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _QA_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.3},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("message", {}).get("content", "{}")

    # Extract JSON from response
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:])
        if raw.endswith("```"):
            raw = raw[:-3]
    return json.loads(raw)


async def run() -> None:
    task_spec = json.loads(_TASK_SPEC_RAW)
    ollama_base_url = task_spec.get("ollama_base_url", _OLLAMA_BASE_URL)
    qa_model = task_spec.get("model", "minimax-m2.7:cloud")
    bundle_branch = task_spec.get("bundle_branch", _BUNDLE_BRANCH)
    repo_path = task_spec.get("repo_path", _REPO_PATH)
    verification_plan = task_spec.get("verification_plan", {})
    auto_pass = task_spec.get("auto_pass", False)
    verification_strategy = task_spec.get("verification_strategy")
    developer_attempts = task_spec.get("developer_verification_attempts", 1)
    bundle_requirements = task_spec.get("bundle_requirements", "")

    rpc = RpcClient()
    await rpc.connect()

    try:
        auth_params = {"token": _TOKEN, "method": "auth"}
        await rpc.call("auth", auth_params)

        # 1. Run deep verification: automated checks + VerificationRunner
        auto_results = _run_automated_checks(repo_path)
        verify_result = await _run_deep_verification(repo_path, verification_strategy)
        code_diff = _get_code_diff(repo_path, bundle_branch)

        # 2. Developer attempt analysis (if needed)
        attempt_analysis = ""
        if developer_attempts > 1 and not auto_pass:
            attempt_analysis = await _analyze_developer_attempts(
                developer_attempts, ollama_base_url, qa_model
            )

        # 3. Build QA evaluation prompt
        prompt = _build_qa_prompt(
            verification_plan, auto_results, code_diff,
            verify_result, attempt_analysis, bundle_requirements,
        )

        # 4. QA scoring + self-fix loop
        acceptance_criteria = verification_plan.get("acceptance_criteria", [])
        if auto_pass:
            report = _auto_pass_report()
            criterion_scores = []
        else:
            report, criterion_scores = await _qa_self_fix_loop(
                prompt, acceptance_criteria, repo_path,
                verification_strategy, ollama_base_url, qa_model,
            )

        # 5. Determine outcome
        all_criteria_pass = all(c.pass_fail for c in criterion_scores) if criterion_scores else True
        llm_outcome = report.get("overall_outcome", "failed")
        if llm_outcome == "passed" and all_criteria_pass:
            final_outcome = "success"
        else:
            final_outcome = "failure"

        final_summary = report.get("summary", "")
        if attempt_analysis:
            final_summary = attempt_analysis + "\n\n" + final_summary

        # 6. Publish Verification Report artifact
        try:
            report_json = json.dumps({
                **report,
                "criterion_scores": _dump_scores(criterion_scores),
                "developer_attempt_analysis": attempt_analysis,
            })
            import base64
            report_b64 = base64.b64encode(report_json.encode()).decode()
            await rpc.call("artifact.publish", {
                "descriptor": {
                    "namespace": "bundle",
                    "name": "verification-report",
                    "version": _BUNDLE_ID,
                    "content_type": "application/json",
                },
                "data": report_b64,
            })
        except Exception:
            pass

        # 7. Send final_report
        await rpc.call("worker.final_report", _format_final_params(
            final_outcome, report, final_summary, criterion_scores,
        ))

    finally:
        await rpc.close()


if __name__ == "__main__":
    asyncio.run(run())


def main() -> None:
    """Entry point for console_scripts."""
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f"QA worker fatal: {exc}", file=sys.stderr)
        sys.exit(1)
