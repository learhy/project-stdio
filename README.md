# The PM War Room

A three-service web application that helps product managers survive endless meetings. Two Flask REST APIs power a React dashboard that delivers threat assessments, meeting survival tips, excuses, massaged metrics, benchmark comparisons, and trend projections — all wrapped in a military-themed dark UI.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## Setup

```bash
docker-compose up
```

## Usage

Open **http://localhost:3000** to use the dashboard.

## Services

| Service | Framework | Port | Purpose |
|---------|-----------|------|---------|
| situation-room | Flask | 5001 | Threat assessment, survival guides, excuse generation |
| metric-massager | Flask | 5002 | Metric spinning, benchmarking, trend projections |
| war-room | React | 3000 | Single-page military-themed dashboard |

## API Endpoints

### situation-room (http://localhost:5001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/threat-assessment` | GET | Returns a random threat level assessment |
| `/meeting-survival-guide/<type>` | GET | Returns survival tips for a meeting type |
| `/excuse-generator` | GET | Returns a random excuse to leave a meeting |

### metric-massager (http://localhost:5002)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/spin` | GET | Reinterprets raw metrics creatively |
| `/benchmark/<value>` | GET | Compares a value against favorable benchmarks |
| `/trend` | GET | Generates optimistic trend projections |
