import random
import time
import re
import json
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request

app = Flask(__name__)

BUNDLE_ID_RE = re.compile(r"^bundle-[a-z0-9]{20}$")
VALID_STATES = {
    "PROPOSED", "IN_REVIEW", "APPROVED", "IN_PROGRESS",
    "PAUSED", "VERIFYING", "COMPLETE", "FAILED", "REJECTED",
}

bundles_store: dict[str, dict[str, Any]] = {}

start_time = time.time()


def _jitter(base: float, pct: float = 0.05) -> float:
    return round(base * (1 + random.uniform(-pct, pct)), 4)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.route("/health", methods=["GET"])
def health():
    uptime = _jitter(time.time() - start_time)
    components = {
        "db": random.choice(["ok", "ok", "ok", "degraded"]),
        "executor": random.choice(["ok", "ok", "ok", "ok", "stalled"]),
        "github": random.choice(["ok", "ok", "disabled"]),
    }
    active = random.randint(0, 12)
    stalled = random.randint(0, max(0, active // 3))
    return jsonify({
        "status": "ok",
        "uptime_seconds": uptime,
        "version": "0.1.0",
        "components": components,
        "bundles": {
            "active": active,
            "stalled": stalled,
            "total": len(bundles_store),
        },
        "timestamp": _now_iso(),
    })


@app.route("/bundles", methods=["GET"])
def list_bundles():
    state_filter = request.args.get("state")
    if state_filter is not None:
        if state_filter not in VALID_STATES:
            return jsonify({"error": f"invalid state filter: {state_filter!r}"}), 400

    limit_raw = request.args.get("limit", "50")
    try:
        limit = int(limit_raw)
    except ValueError:
        return jsonify({"error": f"limit must be an integer, got {limit_raw!r}"}), 400
    if limit < 1 or limit > 200:
        return jsonify({"error": "limit must be between 1 and 200"}), 400

    results = list(bundles_store.values())
    if state_filter:
        results = [b for b in results if b["state"] == state_filter]
    results = sorted(results, key=lambda b: b["created_at"], reverse=True)[:limit]
    return jsonify(results)


@app.route("/bundles/<bundle_id>", methods=["GET"])
def get_bundle(bundle_id: str):
    if not BUNDLE_ID_RE.match(bundle_id):
        return jsonify({"error": f"invalid bundle_id format: {bundle_id!r}"}), 400

    bundle = bundles_store.get(bundle_id)
    if bundle is None:
        return jsonify({"error": "bundle not found"}), 404
    return jsonify(bundle)


@app.route("/submit", methods=["POST"])
def submit():
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "request body must be valid JSON"}), 400

    idea = body.get("idea")
    if not isinstance(idea, str) or not idea.strip():
        return jsonify({"error": "field 'idea' is required and must be a non-empty string"}), 400

    capability = body.get("capability")
    if capability is not None:
        if not isinstance(capability, dict):
            return jsonify({"error": "field 'capability' must be an object if provided"}), 400

    bundle_id = "bundle-" + "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=20))
    delay = _jitter(0.015, pct=0.8)
    time.sleep(delay)

    bundle = {
        "id": bundle_id,
        "idea": idea.strip(),
        "state": "PROPOSED",
        "complexity": random.randint(1, 10),
        "risk": random.randint(1, 10),
        "created_at": _now_iso(),
    }
    if capability is not None:
        bundle["capability"] = capability

    bundles_store[bundle_id] = bundle
    return jsonify(bundle), 201


@app.route("/reset", methods=["POST"])
def reset():
    bundles_store.clear()
    return jsonify({"message": "store cleared"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100, debug=True)
