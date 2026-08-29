package main

import (
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// discardConn is a net.Conn that accepts every write and never reads - enough
// to drive the agent's framing without a socket.
type discardConn struct{}

func (discardConn) Read([]byte) (int, error)         { return 0, net.ErrClosed }
func (discardConn) Write(b []byte) (int, error)      { return len(b), nil }
func (discardConn) Close() error                     { return nil }
func (discardConn) LocalAddr() net.Addr              { return nil }
func (discardConn) RemoteAddr() net.Addr             { return nil }
func (discardConn) SetDeadline(time.Time) error      { return nil }
func (discardConn) SetReadDeadline(time.Time) error  { return nil }
func (discardConn) SetWriteDeadline(time.Time) error { return nil }

// The agent must never PRODUCE a frame the hub cannot accept.
//
// The hub refuses to parse a body it cannot MAC-verify, so on a
// replay-protected connection - every agent holding a per-agent credential,
// i.e. the steady state - an oversize reply CLOSES the connection. A single
// `read_file /var/log/syslog` therefore knocked the host off the hub and
// returned the caller nothing (found live on dev, 2026-08-29). These bound the
// three places a payload enters a frame.

func TestReadFileRefusesAFileLargerThanTheFrameBudget(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", dir)

	big := filepath.Join(dir, "big.log")
	if err := os.WriteFile(big, make([]byte, maxPayloadBytes+1), 0o644); err != nil {
		t.Fatal(err)
	}

	_, err := readFile(big)
	if err == nil {
		t.Fatal("a file over the payload budget must be refused, not read")
	}
	if !strings.Contains(err.Error(), "accepts at most") {
		t.Fatalf("the refusal must name the limit, got %q", err)
	}
	if !strings.Contains(err.Error(), big) {
		t.Fatalf("the refusal must name the path, got %q", err)
	}
}

func TestReadFileStillReturnsAFileAtTheBudget(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", dir)

	ok := filepath.Join(dir, "ok.log")
	if err := os.WriteFile(ok, make([]byte, maxPayloadBytes), 0o644); err != nil {
		t.Fatal(err)
	}
	content, err := readFile(ok)
	if err != nil {
		t.Fatalf("a file exactly at the budget must still be readable: %v", err)
	}
	if len(content) != maxPayloadBytes {
		t.Fatalf("want %d bytes, got %d", maxPayloadBytes, len(content))
	}
}

func TestEncodeMessageRefusesAnOversizeFrame(t *testing.T) {
	_, err := encodeMessage(msg{"content": strings.Repeat("x", maxMessageSize+16)})
	if !errors.Is(err, errFrameTooLarge) {
		t.Fatalf("want errFrameTooLarge, got %v", err)
	}
}

func TestCapWriterTruncatesAndSaysSo(t *testing.T) {
	w := &capWriter{limit: 1024}
	if _, err := w.Write(make([]byte, 5000)); err != nil {
		t.Fatal(err)
	}
	out := w.String()
	if len(out) > 1024+512 {
		t.Fatalf("captured output must stay near the limit, got %d bytes", len(out))
	}
	if !strings.Contains(out, "output truncated") {
		t.Fatal("a truncated stream must say it was truncated")
	}
	if !strings.Contains(out, "5000 bytes produced") {
		t.Fatalf("the notice must state the true size, got %q", out)
	}
}

func TestCapWriterLeavesShortOutputAlone(t *testing.T) {
	w := &capWriter{limit: 1024}
	if _, err := w.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	if got := w.String(); got != "hello" {
		t.Fatalf("want %q, got %q", "hello", got)
	}
}

func TestExecOutputIsBoundedByTheStreamBudget(t *testing.T) {
	// `ls` on a directory of many long names is bounded in practice, so drive
	// the writer directly through Exec's plumbing instead: what matters is that
	// neither stream can claim the whole frame.
	if execStreamBudget*2 > maxPayloadBytes {
		t.Fatalf("stdout+stderr (%d) must fit the payload budget (%d)",
			execStreamBudget*2, maxPayloadBytes)
	}
	if maxPayloadBytes >= maxMessageSize {
		t.Fatalf("the payload budget (%d) must leave room for the envelope under %d",
			maxPayloadBytes, maxMessageSize)
	}
}

// A frame that fails to encode must give its sequence number back: the hub
// verifies sequences fail-closed, so a consumed-but-unsent seq would make every
// later frame look like a replay and cost the agent the connection - turning
// "this answer is too big" into "this host is gone".
func TestAnUnsendableFrameDoesNotConsumeASequenceNumber(t *testing.T) {
	a := &Agent{replayOn: true, replayKey: []byte("k"), conn: discardConn{}}

	if err := a.send(msg{"action": "heartbeat"}); err != nil {
		t.Fatalf("a normal frame must send: %v", err)
	}
	afterGood := a.sendSeq

	err := a.send(msg{"action": "command_result", "content": strings.Repeat("x", maxMessageSize+16)})
	if !errors.Is(err, errFrameTooLarge) {
		t.Fatalf("want errFrameTooLarge, got %v", err)
	}
	if a.sendSeq != afterGood {
		t.Fatalf("sequence advanced on an unsent frame: %d -> %d", afterGood, a.sendSeq)
	}

	if err := a.send(msg{"action": "heartbeat"}); err != nil {
		t.Fatalf("the next frame must still send: %v", err)
	}
	if a.sendSeq != afterGood+1 {
		t.Fatalf("want seq %d, got %d", afterGood+1, a.sendSeq)
	}
}
