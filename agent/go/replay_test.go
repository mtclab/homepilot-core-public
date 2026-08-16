package main

import (
	"net"
	"testing"
	"time"
)

// pinnedKey / pinnedVector / pinnedHex are the cross-language contract: the hub's
// tests/test_agent_hub_replay.py asserts the SAME canonical string and hex for
// the SAME input. If the two canonicalizations ever drift, one of these tests
// goes red. The vector deliberately includes '<', '>' and '&' (which must stay
// RAW, not HTML-escaped) and a "mac" key (which must be EXCLUDED).
const (
	pinnedKey = "per-agent-token-key"
	pinnedHex = "f5b7f7351c289d5f2fe9af6e42f0a362f343b65871c8b9b6e8a2e9fa3897a6a1"
)

func pinnedFrame() msg {
	return msg{
		"action":     "exec",
		"command":    "echo a > b && cat < c",
		"request_id": "r-1",
		"seq":        1,
		"mac":        "EXCLUDED",
	}
}

// (i) The Go canonicalization + MAC must produce the exact hex the Python hub
// produces for the same dict + key. Revert-check: drop enc.SetEscapeHTML(false)
// in canonicalBytes (or stop excluding "mac") and this fails.
func TestCanonicalizationVectorMatchesPython(t *testing.T) {
	cb, err := canonicalBytes(pinnedFrame())
	if err != nil {
		t.Fatalf("canonicalBytes: %v", err)
	}
	const wantCanon = `{"action":"exec","command":"echo a > b && cat < c","request_id":"r-1","seq":1}`
	if string(cb) != wantCanon {
		t.Fatalf("canonical mismatch:\n got %q\nwant %q", string(cb), wantCanon)
	}
	got, err := computeMAC([]byte(pinnedKey), pinnedFrame())
	if err != nil {
		t.Fatalf("computeMAC: %v", err)
	}
	if got != pinnedHex {
		t.Fatalf("hex mismatch: got %s want %s", got, pinnedHex)
	}
}

// (h.1) When the agent holds a per-agent token it must advertise replay in the
// register frame: "replay":1 plus a >=32-hex-char nonce and an integer ts.
// Revert-check: remove the negotiateReplay block in register() and this fails.
func TestRegisterFrameNegotiatesReplayWhenPerAgent(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()

	a := &Agent{cfg: Config{AuthToken: "pa-token"}, agentID: "agent-x", hasPerAgent: true}
	a.conn = client

	got := make(chan msg, 1)
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		m, err := readMessage(server)
		if err == nil {
			ack, _ := encodeMessage(msg{
				"action":   "register_ack",
				"agent_id": "agent-x",
				"features": []any{"per-agent-creds", "replay-v1"},
			})
			_, _ = server.Write(ack)
		}
		got <- m
	}()

	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}
	m := <-got
	if m["replay"] != float64(1) {
		t.Fatalf("expected replay=1, got %v", m["replay"])
	}
	nonce, _ := m["nonce"].(string)
	if len(nonce) < 32 {
		t.Fatalf("expected nonce >=32 hex chars, got %q", nonce)
	}
	if _, ok := m["ts"].(float64); !ok {
		t.Fatalf("expected integer ts, got %v", m["ts"])
	}
	// The ack advertised replay-v1, so framing is now enabled for this connection.
	if !a.replayOn {
		t.Fatal("replayOn should be true after ack advertised replay-v1")
	}
	if a.recvExpected != 1 {
		t.Fatalf("recvExpected should start at 1, got %d", a.recvExpected)
	}
	if string(a.replayKey) != "pa-token" {
		t.Fatalf("replayKey should be the per-agent token, got %q", string(a.replayKey))
	}
}

// (h.2) An enrolling agent (no per-agent token yet) must NOT add replay fields —
// back-compat, and it has no MAC key. Revert-check: make register() always
// negotiate and this fails.
func TestRegisterFrameOmitsReplayWithoutPerAgentToken(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()

	a := &Agent{cfg: Config{AuthToken: "shared"}, agentID: "agent-x", hasPerAgent: false}
	a.conn = client

	got := make(chan msg, 1)
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		m, err := readMessage(server)
		if err == nil {
			ack, _ := encodeMessage(msg{"action": "register_ack", "agent_id": "agent-x"})
			_, _ = server.Write(ack)
		}
		got <- m
	}()

	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}
	m := <-got
	if _, ok := m["replay"]; ok {
		t.Fatal("register frame must not carry replay when no per-agent token is held")
	}
	if _, ok := m["nonce"]; ok {
		t.Fatal("register frame must not carry a nonce without replay")
	}
	if a.replayOn {
		t.Fatal("replayOn must stay false without a per-agent token")
	}
}

// (h.3) On a replay-enabled connection, send() stamps a monotonic seq (from 1)
// and a valid MAC, and verifyInbound accepts a correctly stamped frame while
// rejecting a bad seq or a bad MAC. Revert-check: remove the a.replayOn block in
// send() and the stamped-seq assertion fails.
func TestReplaySendStampsAndVerifyInbound(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()

	a := &Agent{cfg: Config{}}
	a.conn = client
	a.replayOn = true
	a.replayKey = []byte("k")

	recv := make(chan msg, 2)
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		for i := 0; i < 2; i++ {
			m, err := readMessage(server)
			if err != nil {
				return
			}
			recv <- m
		}
	}()

	if err := a.send(msg{"action": "heartbeat", "request_id": "h1"}); err != nil {
		t.Fatalf("send 1: %v", err)
	}
	if err := a.send(msg{"action": "heartbeat", "request_id": "h2"}); err != nil {
		t.Fatalf("send 2: %v", err)
	}

	m1 := <-recv
	m2 := <-recv
	if m1["seq"] != float64(1) || m2["seq"] != float64(2) {
		t.Fatalf("expected seq 1 then 2, got %v then %v", m1["seq"], m2["seq"])
	}
	// The MAC on the first frame must verify with the same key.
	mac1, _ := m1["mac"].(string)
	if !verifyMAC([]byte("k"), m1, mac1) {
		t.Fatal("stamped MAC did not verify")
	}

	// verifyInbound: a correctly stamped inbound frame (seq 1) is accepted.
	// Inbound seq arrives as float64 (json.Unmarshal), so mirror that here.
	a.recvExpected = 1
	inbound := msg{"action": "exec", "request_id": "r", "seq": float64(1)}
	inbound["mac"], _ = computeMAC(a.replayKey, inbound)
	if err := a.verifyInbound(inbound); err != nil {
		t.Fatalf("valid inbound rejected: %v", err)
	}
	if a.recvExpected != 2 {
		t.Fatalf("recvExpected should advance to 2, got %d", a.recvExpected)
	}
	// A replayed/duplicate seq (still 1) is rejected.
	if err := a.verifyInbound(inbound); err == nil {
		t.Fatal("duplicate seq should be rejected")
	}
	// A tampered MAC is rejected.
	bad := msg{"action": "exec", "request_id": "r", "seq": float64(2), "mac": "deadbeef"}
	a.recvExpected = 2
	if err := a.verifyInbound(bad); err == nil {
		t.Fatal("bad MAC should be rejected")
	}
}
