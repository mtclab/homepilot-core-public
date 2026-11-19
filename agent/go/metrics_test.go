package main

import (
	"testing"
	"time"
)

// The metrics frame's canonical form, pinned across both languages
// (tests/test_agent_hub_replay.py asserts the same string for the same frame).
//
// Teeth: send `batch` as []metricSample instead of asWireSamples(batch) and this
// fails - encoding/json emits a STRUCT's fields in declaration order while the
// hub canonicalises every object with SORTED keys, so a struct here makes every
// metrics frame on a replay-protected connection fail its MAC and drop the
// connection. That is exactly the bug this vector exists to forbid.
const metricsFrameCanonical = `{"action":"metrics","request_id":"r-1","samples":` +
	`[{"clock":1700000000,"metric":"load.1m","value":0.42},` +
	`{"clock":1700000000,"metric":"cpu.count","value":8}],"seq":1}`

func TestMetricsFrameCanonicalFormIsSortedKeyJSON(t *testing.T) {
	frame := msg{
		"action":     "metrics",
		"request_id": "r-1",
		"seq":        1,
		"samples": asWireSamples([]metricSample{
			{Metric: "load.1m", Value: 0.42, Clock: 1700000000},
			{Metric: "cpu.count", Value: 8, Clock: 1700000000},
		}),
	}
	cb, err := canonicalBytes(frame)
	if err != nil {
		t.Fatalf("canonicalBytes: %v", err)
	}
	if string(cb) != metricsFrameCanonical {
		t.Fatalf("canonical mismatch:\n got %q\nwant %q", string(cb), metricsFrameCanonical)
	}
}

func TestSystemInfoToSamplesCoversWhatIsCollected(t *testing.T) {
	samples := systemInfoToSamples(collectSystemInfo(), 1700000000)
	want := []string{
		"disk.total_gb", "disk.free_gb", "disk.used_pct",
		"memory.total_gb", "memory.free_gb", "memory.used_pct",
		"load.1m", "load.5m", "load.15m", "load.1m_per_core",
	}
	got := map[string]bool{}
	for _, s := range samples {
		got[s.Metric] = true
		if s.Clock != 1700000000 {
			t.Errorf("sample %s carries clock %d, want the supplied 1700000000", s.Metric, s.Clock)
		}
	}
	for _, w := range want {
		if !got[w] {
			t.Errorf("collected system info produced no %q sample", w)
		}
	}
}

// #661: cpu.count is a constant that was an eighth of a real metrics table. It
// stays in system_info (the register payload) and is never a sample.
//
// Teeth: put `add("cpu.count", ...)` back in systemInfoToSamples and this fails.
func TestCPUCountIsSystemInfoNotASample(t *testing.T) {
	sys := collectSystemInfo()
	if _, ok := sys["cpu_count"].(int); !ok {
		t.Fatalf("system_info lost cpu_count - the Agents view reads it from there")
	}
	for _, s := range systemInfoToSamples(sys, 1700000000) {
		if s.Metric == "cpu.count" {
			t.Fatalf("cpu.count is sampled again (value %v): it is a constant stored every interval", s.Value)
		}
	}
}

// The ratios travel from system_info into samples verbatim, and a ratio that
// could not be computed is ABSENT rather than a zero a rule would read as "empty".
func TestRatioSamplesComeFromSystemInfoAndAreOmittedWhenUnknown(t *testing.T) {
	sys := map[string]any{
		"disk":   map[string]float64{"total_gb": 20, "free_gb": 2, "used_pct": 90.5},
		"memory": map[string]float64{"total_gb": 4, "free_gb": 1}, // no used_pct: MemTotal was 0
		"load":   map[string]float64{"load_1m": 3, "load_5m": 2, "load_15m": 1, "load_1m_per_core": 1.5},
	}
	got := map[string]float64{}
	for _, s := range systemInfoToSamples(sys, 1) {
		got[s.Metric] = s.Value
	}
	if got["disk.used_pct"] != 90.5 {
		t.Errorf("disk.used_pct = %v, want 90.5", got["disk.used_pct"])
	}
	if got["load.1m_per_core"] != 1.5 {
		t.Errorf("load.1m_per_core = %v, want 1.5", got["load.1m_per_core"])
	}
	if v, ok := got["memory.used_pct"]; ok {
		t.Errorf("memory.used_pct was sampled as %v with no used_pct to report", v)
	}
}

// The exact maths of each ratio, and every zero denominator.
//
// Teeth: drop the `denom == 0` / `totalKB <= 0` / `cpus <= 0` guards and the
// zero cases report NaN or +Inf (which the hub rejects as malformed, losing the
// sample silently) or a fabricated 0; change the formulas and the exact cases fail.
func TestRatioMathIsExactAndDivideByZeroSafe(t *testing.T) {
	// df semantics: used = blocks - bfree; pct = used / (used + bavail).
	// 1000 blocks, 100 free of which 50 are reserved for root: used 900, avail 50.
	if got, ok := diskUsedPct(1000, 100, 50); !ok || got != 94.74 {
		t.Errorf("diskUsedPct(1000,100,50) = %v,%v; want 94.74 (900/950), true", got, ok)
	}
	if got, ok := diskUsedPct(1000, 1000, 1000); !ok || got != 0 {
		t.Errorf("an empty disk = %v,%v; want 0, true", got, ok)
	}
	if got, ok := diskUsedPct(1000, 0, 0); !ok || got != 100 {
		t.Errorf("a full disk = %v,%v; want 100, true", got, ok)
	}
	// Full for a non-root writer although root still has its reserve.
	if got, ok := diskUsedPct(1000, 50, 0); !ok || got != 100 {
		t.Errorf("a disk with only the root reserve left = %v,%v; want 100, true", got, ok)
	}
	if _, ok := diskUsedPct(0, 0, 0); ok {
		t.Errorf("a zero-block filesystem produced a percentage")
	}
	if _, ok := diskUsedPct(10, 20, 0); ok {
		t.Errorf("more free blocks than blocks produced a percentage")
	}

	if got, ok := memUsedPct(8_000_000, 2_000_000); !ok || got != 75 {
		t.Errorf("memUsedPct(8G,2G avail) = %v,%v; want 75, true", got, ok)
	}
	if got, ok := memUsedPct(3, 1); !ok || got != 66.67 {
		t.Errorf("memUsedPct(3,1) = %v,%v; want 66.67, true", got, ok)
	}
	if got, ok := memUsedPct(100, 200); !ok || got != 0 {
		t.Errorf("available above total = %v,%v; want clamped to 0, true", got, ok)
	}
	if _, ok := memUsedPct(0, 0); ok {
		t.Errorf("a zero MemTotal produced a percentage")
	}

	if got, ok := loadPerCore(3, 2); !ok || got != 1.5 {
		t.Errorf("loadPerCore(3, 2) = %v,%v; want 1.5, true", got, ok)
	}
	if got, ok := loadPerCore(4, 64); !ok || got != 0.06 {
		t.Errorf("loadPerCore(4, 64) = %v,%v; want 0.06 (0.0625 rounded), true", got, ok)
	}
	if _, ok := loadPerCore(1, 0); ok {
		t.Errorf("zero CPUs produced a per-core load")
	}
	if _, ok := loadPerCore(-1, 4); ok {
		t.Errorf("a negative load produced a per-core load")
	}
}

// On the real machine the ratio agrees with the absolutes it is reported beside.
func TestCollectedRatiosAgreeWithTheAbsolutes(t *testing.T) {
	sys := collectSystemInfo()
	mem, _ := sys["memory"].(map[string]float64)
	if pct, ok := mem["used_pct"]; ok && mem["total_gb"] > 0 {
		want := 100 * (mem["total_gb"] - mem["free_gb"]) / mem["total_gb"]
		// The absolutes are rounded to 0.01 GB, the ratio is not derived from them.
		if diff := pct - want; diff > 1 || diff < -1 {
			t.Errorf("memory.used_pct %v disagrees with total/free %v/%v (%v)", pct, mem["total_gb"], mem["free_gb"], want)
		}
	}
	if pct, ok := mem["used_pct"]; ok && (pct < 0 || pct > 100) {
		t.Errorf("memory.used_pct %v is outside 0..100", pct)
	}
	disk, _ := sys["disk"].(map[string]float64)
	if pct, ok := disk["used_pct"]; ok && (pct < 0 || pct > 100) {
		t.Errorf("disk.used_pct %v is outside 0..100", pct)
	}
}

func TestBufferDropsOldestFirstPastTheBound(t *testing.T) {
	// The cap floor is maxSamplesPerFrame, so exercise the drop above it.
	capacity := maxSamplesPerFrame
	b := newMetricBuffer(capacity)
	for i := 0; i < capacity; i++ {
		b.add([]metricSample{{Metric: "load.1m", Value: float64(i), Clock: int64(i)}})
	}
	if b.droppedTotal() != 0 {
		t.Fatalf("buffer dropped %d samples before reaching its bound", b.droppedTotal())
	}
	b.add([]metricSample{{Metric: "load.1m", Value: -1, Clock: 9999}})
	if b.len() != capacity {
		t.Errorf("buffer holds %d samples, want the bound %d", b.len(), capacity)
	}
	if b.droppedTotal() != 1 {
		t.Errorf("dropped total = %d, want 1", b.droppedTotal())
	}
	// Oldest-first: sample 0 is gone, sample 1 leads, the newest is retained.
	head := b.peek(1)
	if len(head) != 1 || head[0].Value != 1 {
		t.Errorf("head sample = %+v, want the second-oldest (value 1)", head)
	}
	all := b.peek(capacity)
	if all[len(all)-1].Value != -1 {
		t.Errorf("newest sample = %+v, want the just-added one", all[len(all)-1])
	}
}

func TestUnackedBatchStaysBuffered(t *testing.T) {
	// The whole point of ack-confirmed delivery: a batch the hub never confirmed
	// is still in the buffer, so the next connection re-sends it.
	b := newMetricBuffer(maxSamplesPerFrame)
	b.add([]metricSample{{Metric: "load.1m", Value: 1, Clock: 1}})
	batch, start := b.take(maxSamplesPerFrame)
	if len(batch) != 1 {
		t.Fatalf("take returned %d samples, want 1", len(batch))
	}
	if b.len() != 1 {
		t.Fatalf("take removed samples from the buffer (len=%d)", b.len())
	}
	b.ack(start, len(batch))
	if b.len() != 0 {
		t.Errorf("acked batch was not dropped (len=%d)", b.len())
	}
}

func TestWaitMetricAckIgnoresAStaleAck(t *testing.T) {
	a := &Agent{metrics: newMetricBuffer(maxSamplesPerFrame)}
	done := make(chan struct{})
	acks := make(chan string, 2)
	acks <- "an-older-request"
	acks <- "the-one-we-want"
	if !a.waitMetricAck(done, acks, "the-one-we-want") {
		t.Error("ack for the in-flight batch was not recognised")
	}
}

func TestWaitMetricAckStopsWhenTheConnectionEnds(t *testing.T) {
	a := &Agent{metrics: newMetricBuffer(maxSamplesPerFrame)}
	done := make(chan struct{})
	acks := make(chan string)
	close(done)
	start := time.Now()
	if a.waitMetricAck(done, acks, "req") {
		t.Error("a dead connection must not count as an ack")
	}
	if time.Since(start) > metricAckTimeout {
		t.Error("waitMetricAck waited for the timeout instead of noticing the closed connection")
	}
}

func TestMetricsEnabledByDefaultAndExplicitlyDisablable(t *testing.T) {
	t.Setenv("HP_AGENT_METRICS_ENABLED", "")
	if !ConfigFromEnv().MetricsEnabled {
		t.Error("metrics must be ON when the variable is unset")
	}
	t.Setenv("HP_AGENT_METRICS_ENABLED", "false")
	if ConfigFromEnv().MetricsEnabled {
		t.Error("an explicit false must turn metrics off")
	}
	t.Setenv("HP_AGENT_METRICS_ENABLED", "true")
	t.Setenv("HP_AGENT_METRICS_INTERVAL", "15")
	t.Setenv("HP_AGENT_METRICS_BUFFER", "99")
	cfg := ConfigFromEnv()
	if cfg.MetricsInterval != 15 || cfg.MetricsBuffer != 99 {
		t.Errorf("interval/buffer = %d/%d, want 15/99", cfg.MetricsInterval, cfg.MetricsBuffer)
	}
}
