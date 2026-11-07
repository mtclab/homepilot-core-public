package main

import (
	"fmt"
	"net"
	"strings"
	"testing"
	"time"
)

// #727: the metrics buffer dropped the WRONG samples when it filled during a
// flush, and a zero interval panicked the agent.
//
// flushMetrics peeked a batch, sent it, and on the ack dropped len(batch) from
// the head. If add() evicted the oldest samples meanwhile (buffer full), the
// head had moved: capacity 1000, send 0..499, add 2 (evicting 0 and 1), ack ->
// the drop removed 2..501, so 500 and 501 - never sent - were lost, and the
// eviction of 0 and 1 was logged as a drop although both had been delivered.
//
// HP_AGENT_METRICS_INTERVAL=0 (or HP_AGENT_HEARTBEAT_INTERVAL=0) reached
// time.NewTicker, which panics on a non-positive duration; under the unit's
// StartLimitBurst the agent ended dead with no clear error.
//
// Revert-check: restore peek/dropFront(len(batch)) in flushMetrics and
// TestAFullBufferDuringAFlushDropsOnlyTheAckedSamples fails (the next batch
// starts at 502); remove the interval normalisation and
// TestZeroIntervalsDoNotPanic panics.

func sampleRange(from, to int) []metricSample {
	out := make([]metricSample, 0, to-from)
	for i := from; i < to; i++ {
		out = append(out, metricSample{Metric: "m", Value: float64(i), Clock: int64(i)})
	}
	return out
}

type flushHarness struct {
	a    *Agent
	hub  net.Conn
	acks chan string
	done chan struct{}
	out  chan struct{}
}

func newFlushHarness(t *testing.T, capacity int) *flushHarness {
	t.Helper()
	hub, agentSide := net.Pipe()
	t.Cleanup(func() { _ = hub.Close(); _ = agentSide.Close() })
	a := &Agent{cfg: Config{MetricsEnabled: true}, metrics: newMetricBuffer(capacity), conn: agentSide}
	return &flushHarness{a: a, hub: hub, acks: make(chan string, 4), done: make(chan struct{}),
		out: make(chan struct{})}
}

func (h *flushHarness) start() {
	go func() {
		h.a.flushMetrics(h.done, h.acks, h.a.currentGen())
		close(h.out)
	}()
}

// frame reads one metrics frame and returns its request id and sample clocks.
func (h *flushHarness) frame(t *testing.T) (string, []int64) {
	t.Helper()
	_ = h.hub.SetReadDeadline(time.Now().Add(5 * time.Second))
	m, err := readMessage(h.hub)
	if err != nil {
		t.Fatalf("no metrics frame: %v", err)
	}
	raw, _ := m["samples"].([]any)
	clocks := make([]int64, 0, len(raw))
	for _, s := range raw {
		sm, _ := s.(map[string]any)
		c, _ := sm["clock"].(float64)
		clocks = append(clocks, int64(c))
	}
	return str(m, "request_id"), clocks
}

func TestAFullBufferDuringAFlushDropsOnlyTheAckedSamples(t *testing.T) {
	h := newFlushHarness(t, 1000)
	h.a.metrics.add(sampleRange(0, 1000))
	h.start()

	reqID, first := h.frame(t)
	if len(first) != maxSamplesPerFrame || first[0] != 0 {
		t.Fatalf("first batch = %d samples from %v", len(first), first[:1])
	}
	// While that batch is in flight the sampler adds two, evicting 0 and 1.
	h.a.metrics.add(sampleRange(1000, 1002))
	h.acks <- reqID

	_, second := h.frame(t)
	if len(second) == 0 || second[0] != 500 {
		got := "nothing"
		if len(second) > 0 {
			got = fmt.Sprint(second[0])
		}
		t.Fatalf("after acking 0..499 the next batch starts at %s, want 500 "+
			"(samples 500 and 501 were dropped without ever being sent)", got)
	}
	close(h.done)
	<-h.out
	if n := h.a.metrics.len(); n != 502 {
		t.Fatalf("buffer holds %d samples after the ack, want 502 (500..1001)", n)
	}
	if d := h.a.metrics.droppedTotal(); d != 0 {
		t.Fatalf("%d sample(s) counted as dropped, but the evicted ones had been delivered", d)
	}
}

// If the in-flight batch is never acknowledged, the samples evicted from it
// really are gone - and are counted.
func TestAnUnackedBatchCountsItsEvictedSamplesAsDropped(t *testing.T) {
	h := newFlushHarness(t, 1000)
	h.a.metrics.add(sampleRange(0, 1000))
	h.start()
	h.frame(t)
	h.a.metrics.add(sampleRange(1000, 1002))
	close(h.done) // the connection ends before the ack
	<-h.out

	if d := h.a.metrics.droppedTotal(); d != 2 {
		t.Fatalf("dropped = %d, want 2 (samples 0 and 1 were evicted and never acknowledged)", d)
	}
	head := h.a.metrics.samples[0].Clock
	if head != 2 || h.a.metrics.len() != 1000 {
		t.Fatalf("buffer = %d samples from %d, want 1000 from 2", h.a.metrics.len(), head)
	}
}

func TestZeroIntervalsDoNotPanic(t *testing.T) {
	t.Setenv("HP_AGENT_METRICS_INTERVAL", "0")
	t.Setenv("HP_AGENT_HEARTBEAT_INTERVAL", "-3")
	cfg := ConfigFromEnv()
	warnings := normaliseIntervals(&cfg)
	if cfg.MetricsInterval <= 0 || cfg.HeartbeatInterval <= 0 {
		t.Fatalf("intervals left non-positive: metrics=%d heartbeat=%d", cfg.MetricsInterval, cfg.HeartbeatInterval)
	}
	joined := strings.Join(warnings, "\n")
	for _, name := range []string{"HP_AGENT_METRICS_INTERVAL", "HP_AGENT_HEARTBEAT_INTERVAL"} {
		if !strings.Contains(joined, name) {
			t.Fatalf("no startup warning names %s:\n%s", name, joined)
		}
	}

	// And the loops themselves never hand NewTicker a non-positive duration,
	// even for an Agent built without ConfigFromEnv.
	a := &Agent{cfg: Config{MetricsEnabled: true, MetricsInterval: 0, HeartbeatInterval: 0},
		metrics: newMetricBuffer(maxSamplesPerFrame)}
	stop := make(chan struct{})
	close(stop)
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("a zero metrics interval panicked the sampler: %v", r)
			}
		}()
		a.samplerLoop(stop)
	}()
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("a zero heartbeat interval panicked the heartbeat loop: %v", r)
			}
		}()
		a.heartbeatLoop(stop, 0)
	}()
}

func TestIntervalsAreBounded(t *testing.T) {
	cfg := Config{MetricsInterval: 10_000_000, HeartbeatInterval: 10_000}
	normaliseIntervals(&cfg)
	if cfg.HeartbeatInterval > maxHeartbeatInterval {
		t.Fatalf("heartbeat interval %d exceeds %d: the hub drops a connection silent for 300 s",
			cfg.HeartbeatInterval, maxHeartbeatInterval)
	}
	if cfg.MetricsInterval > maxMetricsInterval {
		t.Fatalf("metrics interval %d exceeds %d", cfg.MetricsInterval, maxMetricsInterval)
	}
	ok := Config{MetricsInterval: 60, HeartbeatInterval: 30}
	if w := normaliseIntervals(&ok); len(w) != 0 || ok.MetricsInterval != 60 || ok.HeartbeatInterval != 30 {
		t.Fatalf("valid intervals were touched: %+v %v", ok, w)
	}
}
