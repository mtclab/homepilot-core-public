package main

import (
	"log"
	"sync"
	"time"
)

// Native metrics (ADR-004 S5). The agent reports what collectSystemInfo already
// gathers over the hub connection it already owns. Nothing here derives or
// estimates a value the host did not report.

type metricSample struct {
	Metric string  `json:"metric"`
	Value  float64 `json:"value"`
	Clock  int64   `json:"clock"`
}

// maxSamplesPerFrame keeps one metrics frame far under the hub's 1 MiB limit
// (a sample serialises to well under 100 bytes) and bounds how much a single
// failed send can hold up.
const maxSamplesPerFrame = 500

// metricAckTimeout bounds the wait for the hub's metrics_ack. A batch is only
// dropped from the buffer once the hub has acknowledged it, so a connection
// that dies mid-write re-sends rather than punching a hole in the series.
const metricAckTimeout = 30 * time.Second

// asWireSamples converts samples to maps before they go into a frame.
//
// This is NOT cosmetic. On a replay-protected connection the frame is MAC'd over
// its canonical sorted-key JSON (see replay.go), and encoding/json emits a MAP's
// keys sorted but a STRUCT's fields in declaration order. A struct here would
// therefore produce bytes the hub cannot reproduce, and every single metrics
// frame would fail its MAC and cost the agent the connection. The pinned
// canonical form is asserted in metrics_test.go and tests/test_agent_hub_replay.py.
func asWireSamples(samples []metricSample) []map[string]any {
	out := make([]map[string]any, 0, len(samples))
	for _, s := range samples {
		out = append(out, map[string]any{
			"metric": s.Metric,
			"value":  s.Value,
			"clock":  s.Clock,
		})
	}
	return out
}

// systemInfoToSamples reshapes one collectSystemInfo() snapshot into samples.
// Metric names are the storage keys; keep them stable.
func systemInfoToSamples(sys map[string]any, now int64) []metricSample {
	out := make([]metricSample, 0, 8)
	add := func(key string, v float64) {
		out = append(out, metricSample{Metric: key, Value: v, Clock: now})
	}
	if cc, ok := sys["cpu_count"].(int); ok {
		add("cpu.count", float64(cc))
	}
	if disk, ok := sys["disk"].(map[string]float64); ok && len(disk) > 0 {
		add("disk.total_gb", disk["total_gb"])
		add("disk.free_gb", disk["free_gb"])
	}
	if mem, ok := sys["memory"].(map[string]float64); ok && len(mem) > 0 {
		add("memory.total_gb", mem["total_gb"])
		add("memory.free_gb", mem["free_gb"])
	}
	if load, ok := sys["load"].(map[string]float64); ok && len(load) > 0 {
		add("load.1m", load["load_1m"])
		add("load.5m", load["load_5m"])
		add("load.15m", load["load_15m"])
	}
	return out
}

// metricBuffer is a bounded FIFO of samples awaiting delivery. It survives
// reconnects (it lives on the Agent, not on a connection), so a hub restart
// costs latency rather than data.
//
// The bound is not optional: an agent that cannot reach its hub for a week must
// not grow without limit on a host it does not own. When the bound is reached
// the OLDEST samples are dropped first (recent data is what an operator acts
// on) and every drop is logged with a running total - a silent hole is exactly
// the failure this buffer exists to prevent.
type metricBuffer struct {
	mu       sync.Mutex
	samples  []metricSample
	capacity int
	dropped  uint64
}

func newMetricBuffer(capacity int) *metricBuffer {
	if capacity < maxSamplesPerFrame {
		capacity = maxSamplesPerFrame
	}
	return &metricBuffer{capacity: capacity}
}

// add appends samples, discarding oldest-first past the cap. Returns how many
// were discarded by THIS call.
func (b *metricBuffer) add(s []metricSample) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.samples = append(b.samples, s...)
	over := len(b.samples) - b.capacity
	if over <= 0 {
		return 0
	}
	b.samples = append(b.samples[:0], b.samples[over:]...)
	b.dropped += uint64(over)
	log.Printf("metrics buffer full (%d samples): dropped %d oldest sample(s), %d dropped since start",
		b.capacity, over, b.dropped)
	return over
}

// peek returns a copy of up to n leading samples without removing them.
func (b *metricBuffer) peek(n int) []metricSample {
	b.mu.Lock()
	defer b.mu.Unlock()
	if n > len(b.samples) {
		n = len(b.samples)
	}
	if n == 0 {
		return nil
	}
	out := make([]metricSample, n)
	copy(out, b.samples[:n])
	return out
}

// dropFront removes n delivered samples from the head.
func (b *metricBuffer) dropFront(n int) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if n > len(b.samples) {
		n = len(b.samples)
	}
	b.samples = append(b.samples[:0], b.samples[n:]...)
}

func (b *metricBuffer) len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.samples)
}

func (b *metricBuffer) droppedTotal() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.dropped
}

// samplerLoop runs for the whole PROCESS lifetime, not per connection.
//
// That split is the point of the buffer: sampling has to continue while the hub
// is unreachable, or a hub restart still punches a hole in the series and the
// buffer only ever protects the one batch that was in flight. The flusher below
// is the half that is per-connection.
func (a *Agent) samplerLoop(stop <-chan struct{}) {
	if !a.cfg.MetricsEnabled {
		return
	}
	sample := func() {
		a.metrics.add(systemInfoToSamples(collectSystemInfo(), time.Now().Unix()))
	}
	sample() // a fresh agent reports now, not one interval from now
	t := time.NewTicker(time.Duration(a.cfg.MetricsInterval) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			sample()
		}
	}
}

// metricsLoop flushes the buffer over ONE connection. It flushes immediately on
// entry, so a reconnect delivers the whole backlog at once rather than waiting
// out an interval.
func (a *Agent) metricsLoop(done <-chan struct{}, acks <-chan string, gen uint64) {
	if !a.cfg.MetricsEnabled {
		return
	}
	a.flushMetrics(done, acks, gen)
	t := time.NewTicker(time.Duration(a.cfg.MetricsInterval) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			a.flushMetrics(done, acks, gen)
		}
	}
}

// flushMetrics sends buffered samples in frames of at most maxSamplesPerFrame,
// dropping each batch from the buffer only after the hub acks it. Returns as
// soon as anything fails so the samples stay buffered for the next attempt.
func (a *Agent) flushMetrics(done <-chan struct{}, acks <-chan string, gen uint64) {
	for a.metrics.len() > 0 {
		batch := a.metrics.peek(maxSamplesPerFrame)
		reqID := newUUID()
		if err := a.sendOn(gen, msg{
			"action": "metrics", "samples": asWireSamples(batch), "request_id": reqID,
		}); err != nil {
			log.Printf("metrics send failed (%v); %d sample(s) buffered", err, a.metrics.len())
			return
		}
		if !a.waitMetricAck(done, acks, reqID) {
			return
		}
		a.metrics.dropFront(len(batch))
	}
}

// waitMetricAck blocks until the hub acks reqID, the connection ends, or the
// timeout expires. Acks for other request ids are drained (a stale ack from a
// previous attempt must not satisfy this one).
func (a *Agent) waitMetricAck(done <-chan struct{}, acks <-chan string, reqID string) bool {
	deadline := time.NewTimer(metricAckTimeout)
	defer deadline.Stop()
	for {
		select {
		case <-done:
			return false
		case <-deadline.C:
			log.Printf("metrics ack timed out; %d sample(s) stay buffered", a.metrics.len())
			return false
		case got, ok := <-acks:
			if !ok {
				return false
			}
			if got == reqID {
				return true
			}
		}
	}
}
