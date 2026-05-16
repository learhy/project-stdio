# Phase 6: Developer Worker Self-Healing Loop and Artifact-Aware Verification

## Background and motivation

Field research (Devin, SWE-agent, OpenHands, KiloCode) confirms that the dominant pattern for autonomous code quality is a single agent with a tight write-test-fix inner loop rather than handoffs between specialized workers. The empirical finding is that context retention beats role specialization for mechanical verification tasks. A developer worker that immediately tests its own output and fixes failures in the same session outperforms a separate QA worker receiving a cold handoff of generated code.

However, project-stdio's existing QA worker design is not wrong — it solves a different problem. The QA worker is the governance layer: did the code satisfy the acceptance criteria, match the plan, handle edge cases, integrate correctly with the existing codebase? That requires structured evaluation against pre-execution commitments, which the calibration loop depends on. The QA worker stays.

Phase 6 adds what's missing: the developer worker's own write-test-fix inner loop before it ever commits, so the QA worker receives code that already passes its own smoke tests rather than raw first-draft output.

The architecture becomes:

```
Developer worker:
  write code
  → inner loop: verify → if fails → fix → verify again → (up to N attempts)
  → commit only when verification passes (or attempts exhausted + escalate)

QA worker (after commit, unchanged):
  deep verification against acceptance criteria
  → calibration data
  → pass/fail gate before PR opens
```

---

## Bundle 6.1: Artifact classification and verification strategy

### Background

Not all workers produce executable artifacts. The developer worker needs to know what it built and what kind of verification is appropriate before running any tests. A Flask app needs a smoke test. A Python library needs pytest. A Dockerfile needs `docker build --no-cache`. Documentation needs an LLM review pass. Running the wrong verification strategy is either useless (linting a Flask app without running it) or misleading (running pytest on a project that has no tests yet).

The bundler already classifies what it's building — that classification should flow through to the developer worker as a first-class field in the task spec.

### Artifact types

Define an `ArtifactType` enum in `studio/orchestrator/artifacts.py`:

```python
class ArtifactType(str, Enum):
    EXECUTABLE_APP = "executable_app"      # Flask, FastAPI, CLI tools, scripts
    LIBRARY = "library"                     # Python packages, npm packages
    INFRASTRUCTURE = "infrastructure"       # Dockerfile, Helm chart, Terraform
    DOCUMENTATION = "documentation"         # README, API docs, specs
    DATA_SCHEMA = "data_schema"            # SQL migrations, JSON schemas, OpenAPI specs
    TEST_SUITE = "test_suite"              # Tests only (no production code)
    MIXED = "mixed"                        # Multiple types in one bundle
```

### Verification strategies

For each artifact type, define the verification strategy in `studio/workers/verification.py`:

**EXECUTABLE_APP:**
1. Install dependencies: `pip install -r requirements.txt` (or `npm install`, etc.)
2. Start the application in the background
3. Wait for it to be ready (poll health endpoint or use startup timeout)
4. Run smoke test: hit each documented endpoint, verify status codes and response shapes
5. Tear down

**LIBRARY:**
1. Install the package: `pip install -e .`
2. Run test suite: `pytest` (or `npm test`, `cargo test`, etc.)
3. If no tests exist: FAIL with message "Library has no test coverage — worker must write tests before committing"

**INFRASTRUCTURE:**
1. For Dockerfile: `docker build --no-cache -t smoke-test-{bundle-id} .` then `docker rmi smoke-test-{bundle-id}`
2. For Helm: `helm lint .`
3. For Terraform: `terraform init && terraform validate`

**DOCUMENTATION:**
1. LLM review pass: does the documentation accurately describe the code? Are examples correct? Is it complete?
2. Check for broken internal links
3. Verify any code examples in the docs are syntactically valid

**DATA_SCHEMA:**
1. For SQL migrations: dry-run against an in-memory SQLite instance
2. For JSON schemas: validate the schema file itself against the JSON Schema meta-schema
3. For OpenAPI: `openapi-spec-validator spec.yaml`

**MIXED:**
1. Detect which sub-types are present
2. Run the appropriate strategy for each sub-type
3. All must pass

### Bundler changes

The bundler's proposal_json gains a new top-level field:

```json
{
  "artifact_type": "executable_app",
  "verification_strategy": {
    "type": "executable_app",
    "startup_command": "flask run --port 5000",
    "health_check": "GET http://localhost:5000/",
    "smoke_tests": [
      {"method": "GET", "path": "/should-i-have-this-meeting?title=Test", "expected_status": 200},
      {"method": "POST", "path": "/prioritize", "body": {"tasks": ["a", "b"]}, "expected_status": 200}
    ],
    "teardown_command": null
  }
}
```

The bundler generates this as part of its planning pass — it knows what it's building and how to verify it. The developer worker reads `verification_strategy` from the task spec and executes it verbatim. No guessing.

If the bundler doesn't produce a `verification_strategy`, the developer worker defaults to: run `pytest` if tests exist, otherwise skip verification and log a warning.

### Schema

v15 migration: add `artifact_type TEXT` and `verification_strategy_json TEXT` to `dag_nodes` table. The executor passes these through to the worker task spec.

### Tests

- test_artifact_type_detection: bundler produces correct artifact_type for Flask app, Python library, Dockerfile
- test_verification_strategy_flask: smoke test runner hits all declared endpoints, reports pass/fail per endpoint
- test_verification_strategy_library_no_tests: fails with clear "no test coverage" message
- test_verification_strategy_dockerfile: docker build smoke test
- test_verification_strategy_missing: falls back to pytest if verification_strategy absent

Branch: phase-6/artifact-classification. Report ambiguities before coding.

---

## Bundle 6.2: Developer worker self-healing inner loop

### Background

The developer worker currently: runs opencode, calls `_commit_worktree`, reports done. No verification, no self-correction. This bundle adds the write-test-fix inner loop.

### The loop

```python
MAX_ATTEMPTS = 5  # configurable: developer.max_fix_attempts

for attempt in range(1, MAX_ATTEMPTS + 1):
    # Step 1: Write (or fix) the code
    if attempt == 1:
        await self._execute_opencode(objective)
    else:
        await self._execute_opencode(fix_prompt)
    
    # Step 2: Verify
    result = await self._run_verification()
    
    if result.passed:
        # Step 3: Commit and done
        await self._commit_worktree()
        return {"status": "success", "attempts": attempt, "verification": result}
    
    # Step 4: Build fix prompt from failure output
    fix_prompt = self._build_fix_prompt(objective, result.failures, attempt)
    
    # Log intervention for calibration
    await self._report_checkpoint(
        phase_completed=f"Attempt {attempt} failed verification",
        phase_starting=f"Attempt {attempt+1}: fixing {len(result.failures)} failures",
        concerns=[f.summary for f in result.failures]
    )

# All attempts exhausted
await self._escalate_to_pm(objective, result.failures, attempts=MAX_ATTEMPTS)
return {"status": "failed", "attempts": MAX_ATTEMPTS, "verification": result}
```

### Fix prompt construction

The fix prompt is the key. It must be specific enough for opencode to know exactly what to fix:

```python
def _build_fix_prompt(self, original_objective, failures, attempt):
    failure_text = "\n".join([
        f"FAILURE {i+1}: {f.test_name}\n"
        f"  Expected: {f.expected}\n"
        f"  Got: {f.actual}\n"
        f"  Error: {f.error_output}"
        for i, f in enumerate(failures)
    ])
    
    return f"""The code you wrote failed verification on attempt {attempt}.

Original objective: {original_objective}

Verification failures:
{failure_text}

Fix these specific failures. Do not change code that is working correctly.
After fixing, the verification will run again automatically."""
```
