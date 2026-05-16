# PM Sanity API

A Flask microservice that brings levity and questionable wisdom to project management. Four endpoints help you decide whether that meeting could've been an email, recalculate estimates with PM math, generate standup excuses, and prioritize tasks.

## Quick start

```bash
pip install -r requirements.txt
python app.py
```

The server starts on `http://0.0.0.0:5100`.

## Endpoints

### POST /should-i-have-this-meeting

Decides whether a meeting is worth holding.

```bash
curl -s -X POST http://localhost:5100/should-i-have-this-meeting \
  -H "Content-Type: application/json" \
  -d '{"title": "Sync on Q3 roadmap alignment brainstorming session", "attendee_count": 18}'
```

```json
{"answer": "no", "reason": "A meeting with 18 attendees? That's a conference, not a sync. Send a doc."}
```

Missing parameters return a 400 error:

```bash
curl -s -X POST http://localhost:5100/should-i-have-this-meeting \
  -H "Content-Type: application/json" \
  -d '{}'
```

```json
{"error": "field 'title' is required and must be a non-empty string"}
```

### POST /estimate-multiplier

Applies PM-approved multipliers to your optimistic estimates.

```bash
curl -s -X POST http://localhost:5100/estimate-multiplier \
  -H "Content-Type: application/json" \
  -d '{"task_description": "Add a logout button to the settings page", "original_estimate_hours": 2}'
```

```json
{"multiplier": 2.0, "revised_hours": 4.0, "explanation": "It's just a button, but it's never just a button. State management, auth token cleanup, session expiry edge cases, Safari... definitely 2x."}
```

### GET /standup-excuse

Returns a random funny excuse for standup.

```bash
curl -s http://localhost:5100/standup-excuse
```

```json
{"excuse": "Yesterday: deep-dived a rabbit hole. Today: climbing out of said rabbit hole."}
```

### POST /prioritize

Prioritizes a list of tasks by the given criteria.

```bash
curl -s -X POST http://localhost:5100/prioritize \
  -H "Content-Type: application/json" \
  -d '{"tasks": ["Redesign homepage", "Fix login timeout bug", "Update dependencies", "Write deployment docs"], "criteria": "impact"}'
```

```json
{
  "prioritized": [
    {"task": "Fix login timeout bug", "score": 95},
    {"task": "Redesign homepage", "score": 72},
    {"task": "Update dependencies", "score": 48},
    {"task": "Write deployment docs", "score": 31}
  ]
}
```

## Running tests

```bash
python -m pytest test_app.py -v
```

## Requirements

- Python 3.12+
- Flask
- pytest (dev)
