import random

from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


THREAT_LEVELS = [
    {"level": "DEFCON 5", "description": "All clear. No meetings scheduled.", "color": "green"},
    {"level": "DEFCON 4", "description": "Standup in progress. Mild alertness.", "color": "blue"},
    {"level": "DEFCON 3", "description": "Stakeholder sync approaching. Prepare defenses.", "color": "yellow"},
    {"level": "DEFCON 2", "description": "Quarterly planning session detected. High alert.", "color": "orange"},
    {"level": "DEFCON 1", "description": "Emergency all-hands called. Maximum threat.", "color": "red"},
]

SURVIVAL_GUIDES = {
    "status_update": {
        "type": "status_update",
        "tips": [
            "Start with good news to lower everyone's guard.",
            "Use the phrase 'on track' liberally, regardless of reality.",
            "Blame dependencies — they can't defend themselves.",
            "Keep your camera off to conceal panic eating.",
        ],
        "mantra": "Status is a state of mind.",
    },
    "brainstorming": {
        "type": "brainstorming",
        "tips": [
            "Write 'NO BAD IDEAS' on the whiteboard, then judge silently.",
            "Volunteer to be the scribe — you don't have to contribute.",
            "Nod thoughtfully while planning your grocery list.",
            "Suggest a 'quick icebreaker' to burn 15 minutes.",
        ],
        "mantra": "The best idea is the one that ends the meeting.",
    },
    "retro": {
        "type": "retro",
        "tips": [
            "Put everything in 'went well' to avoid uncomfortable conversations.",
            "Blame process, never people (especially yourself).",
            "Suggest more retros to fix the problems found in this retro.",
            "Action items are optional — nobody checks them anyway.",
        ],
        "mantra": "What happens in retro stays in retro.",
    },
    "planning": {
        "type": "planning",
        "tips": [
            "Double every estimate. Triple if marketing is involved.",
            "Point everything as a Fibonacci number — it sounds scientific.",
            "Defer all hard decisions to 'next sprint'.",
            "If asked about capacity, respond with 'it depends'.",
        ],
        "mantra": "A plan is just a list of things that won't happen.",
    },
    "exec-briefing": {
        "type": "exec-briefing",
        "tips": [
            "Lead with the bottom line — execs read nothing else.",
            "Pre-answer the three questions they always ask: cost, timeline, risk.",
            "Have a single slide. Maximum two bullet points. One is better.",
            "If they ask a hard question, say 'we have a tiger team on it'.",
        ],
        "mantra": "If you can't explain it in 30 seconds, it doesn't exist.",
    },
    "all-hands": {
        "type": "all-hands",
        "tips": [
            "Sit in the back row — closer to the exit.",
            "Never ask the first question. Or the second. Or any question.",
            "Clap when everyone else claps. You're a team player.",
            "If called on, say 'great question, let me follow up async'.",
        ],
        "mantra": "All hands on deck, one hand on the door.",
    },
    "one-on-one": {
        "type": "one-on-one",
        "tips": [
            "Always have one 'career goal' ready to deflect from your actual work.",
            "Mention a book you're 'reading' about leadership.",
            "Ask for feedback first — it makes you look coachable.",
            "If they ask about weaknesses, pick something that's secretly a strength.",
        ],
        "mantra": "Your manager is also just trying to survive.",
    },
}

EXCUSES = [
    "\"My dog just unplugged my router.\"",
    "\"I have a hard stop — my plants need watering.\"",
    "\"Gotta drop — my sourdough starter needs feeding.\"",
    "\"Sorry, I'm double-booked with my own sanity.\"",
    "\"My noise-canceling headphones ran out of battery. I can hear everything.\"",
    "\"I need to join another call where my camera is actually required.\"",
    "\"My cat is presenting to executive leadership right now.\"",
    "\"I'm fading — my caffeine levels have dropped below operational threshold.\"",
    "\"I just received a calendar invite for a meeting about this meeting.\"",
    "\"My standing desk won't go down and I can't sit through this.\"",
    "\"I have to return some videotapes.\"",
    "\"My VPN just connected to the wrong dimension.\"",
]


@app.route("/threat-assessment", methods=["POST"])
def threat_assessment():
    body = request.get_json(silent=True) or {}
    stakeholders = body.get("stakeholders", [])
    level = random.choice(THREAT_LEVELS)
    return jsonify({**level, "stakeholders": stakeholders, "headcount": len(stakeholders)})


@app.route("/meeting-survival-guide/<meeting_type>", methods=["GET"])
def meeting_survival_guide(meeting_type):
    guide = SURVIVAL_GUIDES.get(meeting_type)
    if guide is None:
        return jsonify({"error": f"Unknown meeting type: {meeting_type}"}), 404
    return jsonify(guide)


@app.route("/excuse-generator", methods=["POST"])
def excuse_generator():
    body = request.get_json(silent=True) or {}
    missed = body.get("missed", "an unspecified deadline")
    reason = body.get("reason", "reasons beyond my control")
    template = random.choice(EXCUSES).strip('"')
    contextual = f"I missed {missed} because of {reason}. Also, {template}"
    return jsonify({"excuse": contextual, "missed": missed, "blamed_on": reason})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)
