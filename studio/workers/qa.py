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
) -> dict:
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
        },
    }


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

    rpc = RpcClient()
    await rpc.connect()

    try:
        # Authenticate
        auth_params = {"token": _TOKEN, "method": "auth"}
        await rpc.call("auth", auth_params)

        # 1. Run automated checks
        auto_results = _run_automated_checks(repo_path)
        code_diff = _get_code_diff(repo_path, bundle_branch)

        # 2. Build LLM prompt
        prompt = json.dumps({
            "verification_plan": verification_plan,
            "automated_checks": auto_results,
            "code_diff": code_diff,
        }, indent=2)
        if len(prompt) > 24000:
            prompt = prompt[:24000]

        # 3. LLM pass (or auto-pass for local/test environments)
        if auto_pass:
            report = {
                "overall_outcome": "passed",
                "criteria_results": [],
                "failed_criteria": [],
                "recommendations": [],
                "summary": "Auto-passed QA verification (local/test environment)",
            }
        else:
            try:
                report = await _call_llm(prompt, ollama_base_url, qa_model)
            except Exception as exc:
                report = {
                    "overall_outcome": "failed",
                    "criteria_results": [],
                    "failed_criteria": [f"LLM evaluation failed: {exc}"],
                    "recommendations": ["Re-run verification"],
                    "summary": f"QA agent could not complete LLM evaluation: {exc}",
                }

        # 4. Determine outcome
        llm_outcome = report.get("overall_outcome", "failed")
        if llm_outcome == "passed":
            final_outcome = "success"
        else:
            final_outcome = "failure"

        final_summary = report.get("summary", "")

        # 5. Publish Verification Report artifact
        try:
            report_json = json.dumps(report)
            # Convert to base64 for artifact.publish
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
            pass  # best-effort

        # 6. Send final_report
        await rpc.call("worker.final_report", _format_final_params(
            final_outcome, report, final_summary,
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
