"""Artifact classification and verification strategy models.

Separate from artifact.py (storage/GC layer). ArtifactType and VerificationStrategy
are metadata models for the bundler → developer worker pipeline.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ── Verification result models ──────────────────────────────────────────────

class VerificationFailure(BaseModel):
    test_name: str = ""
    expected: str = ""
    actual: str = ""
    error_output: str = ""
    summary: str = ""
    category: str = ""  # missing_dependencies, logic_error, spec_deviation, infrastructure, other


def categorize_failure(error_output: str, summary: str) -> str:
    """Heuristic keyword matching to categorize a verification failure."""
    text = f"{error_output} {summary}".lower()
    if any(kw in text for kw in ("importerror", "modulenotfounderror", "no module named")):
        return "missing_dependencies"
    if any(kw in text for kw in ("assertionerror", "assert")):
        return "logic_error"
    if any(kw in text for kw in ("attributeerror", "keyerror", "typeerror", "valueerror")):
        return "spec_deviation"
    if any(kw in text for kw in ("timeout", "connectionrefused", "connection refused",
                                  "timed out", "connectionerror")):
        return "infrastructure"
    return "other"


class VerificationResult(BaseModel):
    passed: bool = False
    failures: list[VerificationFailure] = Field(default_factory=list)
    output: str = ""
    attempt: int = 1


class ArtifactType(StrEnum):
    EXECUTABLE_APP = "executable_app"      # Flask, FastAPI, CLI tools, scripts
    LIBRARY = "library"                     # Python packages, npm packages
    INFRASTRUCTURE = "infrastructure"       # Dockerfile, Helm chart, Terraform
    DOCUMENTATION = "documentation"         # README, API docs, specs
    DATA_SCHEMA = "data_schema"            # SQL migrations, JSON schemas, OpenAPI specs
    TEST_SUITE = "test_suite"              # Tests only (no production code)
    MIXED = "mixed"                        # Multiple types in one bundle


# ── Verification strategy models ──────────────────────────────────────────────

class SmokeTest(BaseModel):
    method: str = "GET"
    path: str
    body: dict[str, Any] | None = None
    expected_status: int = 200


class VerificationStrategy(BaseModel):
    type: ArtifactType
    # EXECUTABLE_APP
    startup_command: str | None = None
    health_check: str | None = None
    smoke_tests: list[SmokeTest] = Field(default_factory=list)
    teardown_command: str | None = None
    # LIBRARY / general
    test_command: str | None = None
    # INFRASTRUCTURE / DATA_SCHEMA
    validate_command: str | None = None
    # DOCUMENTATION
    review: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> VerificationStrategy:
        smoke = [SmokeTest(**s) for s in d.get("smoke_tests", [])]
        return cls(
            type=ArtifactType(d.get("type", "executable_app")),
            startup_command=d.get("startup_command"),
            health_check=d.get("health_check"),
            smoke_tests=smoke,
            teardown_command=d.get("teardown_command"),
            test_command=d.get("test_command"),
            validate_command=d.get("validate_command"),
            review=d.get("review"),
        )


# ── Criterion scoring ─────────────────────────────────────────────────────


def detect_artifact_type_from_idea(idea: str) -> ArtifactType:
    """Heuristic pre-check to detect the likely artifact type from an idea string.

    Called before the bundler LLM runs so it has a strong prior. The bundler
    can override this, but major mismatches trigger a re-prompt.
    """
    lower = idea.lower()

    # Multi-service signals
    if any(kw in lower for kw in ("docker-compose", "docker compose",
                                    "multiple services", "microservice",
                                    "micro-service", "multi-service")):
        return ArtifactType.MIXED

    # Flask/FastAPI/Django + frontend = MIXED
    web_backend = any(kw in lower for kw in ("flask", "fastapi", "express",
                                               "django", "rails", "gin"))
    web_frontend = any(kw in lower for kw in ("react", "vue", "angular",
                                                "next.js", "svelte", "dashboard"))
    if web_backend and web_frontend:
        return ArtifactType.MIXED

    # Single web backend
    if web_backend:
        return ArtifactType.EXECUTABLE_APP

    # Frontend-only
    if web_frontend:
        return ArtifactType.EXECUTABLE_APP

    # Library signals
    if any(kw in lower for kw in ("library", "package", "module", "sdk",
                                    "pip install", "npm package")):
        return ArtifactType.LIBRARY

    # Infrastructure signals
    if any(kw in lower for kw in ("dockerfile", "helm chart", "terraform",
                                    "kubernetes manifest", "k8s", "ansible")):
        return ArtifactType.INFRASTRUCTURE

    # Documentation-only signals
    if any(kw in lower for kw in ("readme", "api docs", "documentation-only",
                                    "specification document")):
        return ArtifactType.DOCUMENTATION

    # Data schema signals
    if any(kw in lower for kw in ("sql migration", "json schema",
                                    "openapi spec", "graphql schema")):
        return ArtifactType.DATA_SCHEMA

    return ArtifactType.LIBRARY  # conservative fallback


class CriterionScore(BaseModel):
    criterion: str = ""
    score: float = 0.0  # 0.0 to 1.0
    evidence: str = ""
    pass_fail: bool = False
