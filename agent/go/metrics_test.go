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
		"cpu.count",
		"disk.total_gb", "disk.free_gb",
		"memory.total_gb", "memory.free_gb",
		"load.1m", "load.5m", "load.15m",
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
	batch := b.peek(maxSamplesPerFrame)
	if len(batch) != 1 {
		t.Fatalf("peek returned %d samples, want 1", len(batch))
	}
	if b.len() != 1 {
		t.Fatalf("peek removed samples from the buffer (len=%d)", b.len())
	}
	b.dropFront(len(batch))
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
