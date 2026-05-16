import random

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


SPIN_ADJECTIVES = [
    "strategically positioned",
    "growth-oriented",
    "synergistically enhanced",
    "paradigm-shifting",
    "vertically integrated",
    "horizontally scalable",
    "disruptively innovating",
    "exponentially trending",
]

SPIN_VERBS = [
    "showing strong momentum",
    "trending toward breakthrough",
    "exceeding calibrated expectations",
    "demonstrating robust fundamentals",
    "on a trajectory to outperform",
    "exhibiting favorable deviation patterns",
]

BENCHMARKS = [
    {"label": "Fortune 500 median", "percentile": 95},
    {"label": "industry average", "percentile": 88},
    {"label": "FAANG companies", "percentile": 72},
    {"label": "unicorn startups", "percentile": 64},
    {"label": "your biggest competitor", "percentile": 91},
    {"label": "last quarter's numbers", "percentile": 87},
]

TREND_ADVERBS = [
    "conservatively",
    "optimistically",
    "aggressively",
    "exponentially",
    "sustainably",
]

TREND_PROJECTIONS = [
    "continues to dominate the market",
    "surges past all projections",
    "redefines industry standards",
    "achieves hockey-stick growth",
    "leaves competitors in the dust",
    "becomes a case study at Harvard Business Review",
]


@app.route("/spin", methods=["GET"])
def spin():
    adjective = random.choice(SPIN_ADJECTIVES)
    verb = random.choice(SPIN_VERBS)

    raw_input = {
        "revenue_growth": round(random.uniform(-2.0, 3.0), 1),
        "user_acquisition": random.randint(-100, 500),
        "churn_rate": round(random.uniform(1.0, 8.0), 1),
        "nps_score": random.randint(10, 60),
    }

    spun = {}
    for key, value in raw_input.items():
        if value < 0:
            spun[key] = f"{adjective} recalibration in progress, {verb}"
        elif value == 0:
            spun[key] = f"stable at {adjective} baseline, {verb}"
        else:
            spun[key] = f"+{value} ({adjective}, {verb})"

    return jsonify({"raw_metrics": raw_input, "spun_metrics": spun})


@app.route("/benchmark/<value>", methods=["GET"])
def benchmark(value):
    try:
        num = float(value)
    except ValueError:
        return jsonify({"error": f"Invalid benchmark value: {value}"}), 400

    benchmark_ref = random.choice(BENCHMARKS)
    percentile = benchmark_ref["percentile"]
    comparison = round(100 - percentile + random.uniform(-5, 15), 1)

    return jsonify({
        "your_value": num,
        "benchmark": benchmark_ref["label"],
        "you_beat": f"{max(0, min(100, comparison)):.1f}%",
        "verdict": "You are outperforming" if comparison > 50 else "You are strategically on par with",
    })


@app.route("/trend", methods=["GET"])
def trend():
    adverb = random.choice(TREND_ADVERBS)
    projection = random.choice(TREND_PROJECTIONS)

    quarters = ["Q1", "Q2", "Q3", "Q4"]
    data = [
        {"quarter": q, "value": round(random.uniform(70, 140), 1)}
        for q in quarters
    ]

    growth = round(((data[-1]["value"] - data[0]["value"]) / data[0]["value"]) * 100, 1)

    return jsonify({
        "historical_data": data,
        "projection": f"{adverb} {projection}",
        "projected_growth": f"{growth}%",
        "confidence": "This trend line was drawn by someone who believes in you.",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=True)
