import { useEffect, useState, useCallback } from "react";

const API_SITUATION = "http://localhost:5001";
const API_METRICS = "http://localhost:5002";

const styles = {
  app: {
    minHeight: "100vh",
    background: "#0d1117",
    color: "#c9d1d9",
    fontFamily: "'Courier New', Courier, monospace",
    padding: "20px",
  },
  header: {
    borderBottom: "2px solid #58a6ff",
    paddingBottom: "16px",
    marginBottom: "24px",
    display: "flex",
    alignItems: "center",
    gap: "12px",
  },
  title: {
    fontSize: "28px",
    fontWeight: "bold",
    color: "#58a6ff",
    textTransform: "uppercase",
    letterSpacing: "2px",
    margin: 0,
  },
  subtitle: {
    fontSize: "12px",
    color: "#8b949e",
    marginTop: "4px",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
    gap: "20px",
  },
  card: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "8px",
    padding: "20px",
  },
  cardTitle: {
    fontSize: "14px",
    fontWeight: "bold",
    textTransform: "uppercase",
    color: "#58a6ff",
    marginBottom: "12px",
    borderBottom: "1px solid #30363d",
    paddingBottom: "8px",
  },
  threatLevel: (color) => ({
    fontSize: "24px",
    fontWeight: "bold",
    color: color,
    marginBottom: "8px",
  }),
  metricValue: {
    fontSize: "18px",
    color: "#7ee787",
    fontFamily: "monospace",
  },
  tip: {
    padding: "6px 0",
    borderBottom: "1px solid #21262d",
    fontSize: "13px",
    color: "#8b949e",
  },
  button: {
    background: "#238636",
    color: "#fff",
    border: "1px solid #2ea043",
    padding: "8px 16px",
    borderRadius: "6px",
    cursor: "pointer",
    fontFamily: "inherit",
    fontSize: "12px",
    textTransform: "uppercase",
    fontWeight: "bold",
  },
  buttonGroup: {
    display: "flex",
    gap: "8px",
    marginBottom: "12px",
    flexWrap: "wrap",
  },
  excuse: {
    fontSize: "18px",
    fontStyle: "italic",
    color: "#d2a8ff",
    padding: "12px",
    background: "#1c1524",
    borderRadius: "6px",
    border: "1px solid #38304a",
  },
  quarters: {
    display: "flex",
    gap: "8px",
    marginBottom: "12px",
  },
  quarter: {
    flex: 1,
    textAlign: "center",
    background: "#0d1117",
    padding: "8px",
    borderRadius: "4px",
    border: "1px solid #30363d",
  },
  projection: {
    fontSize: "14px",
    color: "#7ee787",
    padding: "10px",
    background: "#0d1117",
    borderRadius: "4px",
    border: "1px solid #30363d",
  },
  loading: {
    textAlign: "center",
    color: "#8b949e",
    padding: "20px",
  },
  error: {
    color: "#f85149",
    fontSize: "12px",
  },
  benchmark: {
    padding: "10px",
    background: "#0d1117",
    borderRadius: "4px",
    border: "1px solid #30363d",
  },
  rawMetric: {
    display: "flex",
    justifyContent: "space-between",
    padding: "4px 0",
    fontSize: "12px",
    borderBottom: "1px solid #21262d",
  },
};

function Card({ title, children }) {
  return (
    <div style={styles.card}>
      <div style={styles.cardTitle}>{title}</div>
      {children}
    </div>
  );
}

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export default function App() {
  const [threat, setThreat] = useState(null);
  const [guide, setGuide] = useState(null);
  const [excuse, setExcuse] = useState(null);
  const [spin, setSpin] = useState(null);
  const [benchmark, setBenchmark] = useState(null);
  const [trend, setTrend] = useState(null);
  const [errors, setErrors] = useState({});

  const loadThreat = useCallback(async () => {
    try {
      setThreat(await fetchJson(`${API_SITUATION}/threat-assessment`));
      setErrors((e) => ({ ...e, threat: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, threat: "Threat assessment offline" }));
    }
  }, []);

  const loadGuide = useCallback(async (type) => {
    try {
      setGuide(await fetchJson(`${API_SITUATION}/meeting-survival-guide/${type}`));
      setErrors((e) => ({ ...e, guide: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, guide: "Survival guide unavailable" }));
    }
  }, []);

  const loadExcuse = useCallback(async () => {
    try {
      setExcuse(await fetchJson(`${API_SITUATION}/excuse-generator`));
      setErrors((e) => ({ ...e, excuse: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, excuse: "Excuse generator offline" }));
    }
  }, []);

  const loadSpin = useCallback(async () => {
    try {
      setSpin(await fetchJson(`${API_METRICS}/spin`));
      setErrors((e) => ({ ...e, spin: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, spin: "Metric massager offline" }));
    }
  }, []);

  const loadBenchmark = useCallback(async (val) => {
    try {
      setBenchmark(await fetchJson(`${API_METRICS}/benchmark/${val}`));
      setErrors((e) => ({ ...e, benchmark: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, benchmark: "Benchmark offline" }));
    }
  }, []);

  const loadTrend = useCallback(async () => {
    try {
      setTrend(await fetchJson(`${API_METRICS}/trend`));
      setErrors((e) => ({ ...e, trend: null }));
    } catch (err) {
      setErrors((e) => ({ ...e, trend: "Trend projections offline" }));
    }
  }, []);

  useEffect(() => {
    loadThreat();
    loadGuide("status_update");
    loadExcuse();
    loadSpin();
    loadBenchmark(42);
    loadTrend();
  }, [loadThreat, loadGuide, loadExcuse, loadSpin, loadBenchmark, loadTrend]);

  return (
    <div style={styles.app}>
      <header style={styles.header}>
        <div>
          <h1 style={styles.title}>The PM War Room</h1>
          <div style={styles.subtitle}>SITUATION ROOM // METRIC MASSAGER // SURVIVAL DASHBOARD</div>
        </div>
      </header>

      <div style={styles.grid}>
        <Card title="Threat Assessment">
          {errors.threat ? (
            <div style={styles.error}>{errors.threat}</div>
          ) : threat ? (
            <>
              <div style={styles.threatLevel(threat.color)}>{threat.level}</div>
              <div>{threat.description}</div>
            </>
          ) : (
            <div style={styles.loading}>Scanning environment...</div>
          )}
        </Card>

        <Card title="Excuse Generator">
          {errors.excuse ? (
            <div style={styles.error}>{errors.excuse}</div>
          ) : excuse ? (
            <div style={styles.excuse}>{excuse.excuse}</div>
          ) : (
            <div style={styles.loading}>Crafting excuse...</div>
          )}
          <button style={{ ...styles.button, marginTop: 12 }} onClick={loadExcuse}>
            Generate New Excuse
          </button>
        </Card>

        <Card title="Meeting Survival Guide">
          <div style={styles.buttonGroup}>
            {["status_update", "brainstorming", "retro", "planning"].map((t) => (
              <button key={t} style={styles.button} onClick={() => loadGuide(t)}>
                {t.replace("_", " ")}
              </button>
            ))}
          </div>
          {errors.guide ? (
            <div style={styles.error}>{errors.guide}</div>
          ) : guide ? (
            <>
              <div style={{ color: "#7ee787", marginBottom: 8, fontWeight: "bold" }}>
                {guide.mantra || "Mantra: "}
              </div>
              {guide.tips && guide.tips.map((tip, i) => (
                <div key={i} style={styles.tip}>
                  &#9656; {tip}
                </div>
              ))}
            </>
          ) : (
            <div style={styles.loading}>Loading survival guide...</div>
          )}
        </Card>

        <Card title="Massaged Metrics">
          <button style={{ ...styles.button, marginBottom: 12 }} onClick={loadSpin}>
            Run Spin Analysis
          </button>
          {errors.spin ? (
            <div style={styles.error}>{errors.spin}</div>
          ) : spin ? (
            <div>
              {Object.entries(spin.raw_metrics).map(([key, val]) => (
                <div key={key} style={styles.rawMetric}>
                  <span style={{ color: "#8b949e" }}>{key.replace(/_/g, " ")}</span>
                  <span style={{ color: "#c9d1d9" }}>{val}</span>
                </div>
              ))}
              <div style={{ marginTop: 12 }}>
                {Object.entries(spin.spun_metrics).map(([key, val]) => (
                  <div key={key} style={{ ...styles.metricValue, fontSize: 13, marginBottom: 4 }}>
                    {key.replace(/_/g, " ")}: {val}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div style={styles.loading}>Massaging metrics...</div>
          )}
        </Card>

        <Card title="Benchmark Comparison">
          {errors.benchmark ? (
            <div style={styles.error}>{errors.benchmark}</div>
          ) : benchmark ? (
            <div style={styles.benchmark}>
              <div style={{ marginBottom: 8 }}>
                <span style={{ color: "#8b949e" }}>Your value: </span>
                <span style={{ color: "#58a6ff" }}>{benchmark.your_value}</span>
              </div>
              <div style={{ marginBottom: 8 }}>
                <span style={{ color: "#8b949e" }}>vs {benchmark.benchmark}: </span>
                <span style={{ color: "#7ee787" }}>{benchmark.verdict}</span>
              </div>
              <div>
                <span style={{ color: "#8b949e" }}>You beat: </span>
                <span style={{ color: "#d2a8ff", fontWeight: "bold" }}>{benchmark.you_beat}</span>
              </div>
            </div>
          ) : (
            <div style={styles.loading}>Loading benchmarks...</div>
          )}
        </Card>

        <Card title="Trend Projections">
          <button style={{ ...styles.button, marginBottom: 12 }} onClick={loadTrend}>
            Refresh Trends
          </button>
          {errors.trend ? (
            <div style={styles.error}>{errors.trend}</div>
          ) : trend ? (
            <div>
              <div style={styles.quarters}>
                {trend.historical_data.map((q) => (
                  <div key={q.quarter} style={styles.quarter}>
                    <div style={{ fontSize: 11, color: "#8b949e" }}>{q.quarter}</div>
                    <div style={{ fontSize: 16, color: "#58a6ff", fontWeight: "bold" }}>{q.value}</div>
                  </div>
                ))}
              </div>
              <div style={styles.projection}>{trend.projection}</div>
              <div style={{ marginTop: 8, fontSize: 13, color: "#8b949e", fontStyle: "italic" }}>
                {trend.confidence}
              </div>
            </div>
          ) : (
            <div style={styles.loading}>Generating projections...</div>
          )}
        </Card>
      </div>
    </div>
  );
}
