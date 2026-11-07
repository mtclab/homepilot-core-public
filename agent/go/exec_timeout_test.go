package main

import (
	"errors"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// #720: a timed-out exec must actually return, and one slow request must not
// wedge the agent.
//
// Found in the 2026-09-24 audit: `bash -c "sleep 8; echo done"` with a 1 s
// timeout came back after 8 s. exec.CommandContext killed bash, but the sleep it
// had started still held stdout, and cmd.Wait blocks until every writer of the
// pipe is gone. serve() dispatched every request inline, so while that one
// command overran nothing else was answered - metric acks timed out - while the
// heartbeat goroutine kept reporting the agent healthy. The timeout itself was
// taken unbounded from the hub.
//
// Revert-check: drop Setpgid/the group kill/WaitDelay from runCommand and
// TestATimedOutCommandReturnsAndLeavesNoChild fails on the elapsed bound and the
// surviving child; dispatch inline again in serve() and
// TestASlowRequestDoesNotBlockTheNextOne times out waiting for the read_file
// answer.

// stubCommand puts an executable named name on PATH running body (sh), and
// returns the state dir the stub can record into.
func stubCommand(t *testing.T, name, body string) string {
	t.Helper()
	bin := t.TempDir()
	state := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, name), []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_TEST_STATE", state)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	return state
}

func processAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil || errors.Is(err, syscall.EPERM)
}

func TestATimedOutCommandReturnsAndLeavesNoChild(t *testing.T) {
	state := stubCommand(t, "hostname",
		`sleep 6 &
echo $! > "$HP_TEST_STATE/child.pid"
wait
echo done
`)
	start := time.Now()
	code, stdout, stderr := Executor{allow: Allowlist{}}.Exec("hostname", 1)
	elapsed := time.Since(start)

	if elapsed > 3*time.Second {
		t.Fatalf("a 1s-timeout exec took %v to return (the child held the pipe open)", elapsed)
	}
	if code != -1 || !strings.Contains(stderr, "timed out") || strings.Contains(stdout, "done") {
		t.Fatalf("expected a timeout result, got code=%d stdout=%q stderr=%q", code, stdout, stderr)
	}
	raw, err := os.ReadFile(filepath.Join(state, "child.pid"))
	if err != nil {
		t.Fatalf("stub never started its child: %v", err)
	}
	pid, _ := strconv.Atoi(strings.TrimSpace(string(raw)))
	deadline := time.Now().Add(2 * time.Second)
	for processAlive(pid) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if processAlive(pid) {
		_ = syscall.Kill(pid, syscall.SIGKILL)
		t.Fatalf("the timed-out command's child %d is still running", pid)
	}
}

func TestExecTimeoutIsClamped(t *testing.T) {
	for in, want := range map[int]time.Duration{
		0:       defaultExecTimeout,
		-5:      defaultExecTimeout,
		1:       time.Second,
		60:      time.Minute,
		600:     maxExecTimeout,
		100000:  maxExecTimeout,
		1 << 40: maxExecTimeout,
	} {
		if got := clampExecTimeout(in); got != want {
			t.Errorf("clampExecTimeout(%d) = %v, want %v", in, got, want)
		}
	}
}

// serveOver starts the shipped serve() loop on an in-memory connection.
func serveOver(t *testing.T) (send func(msg), recv func(time.Duration) msg) {
	t.Helper()
	hub, agentSide := net.Pipe()
	a := &Agent{cfg: Config{HeartbeatInterval: 3600}, exec: Executor{allow: Allowlist{}}, conn: agentSide}
	go func() { _ = a.serve() }()
	t.Cleanup(func() { _ = hub.Close(); _ = agentSide.Close() })
	send = func(m msg) {
		b, err := encodeMessage(m)
		if err != nil {
			t.Fatal(err)
		}
		_ = hub.SetWriteDeadline(time.Now().Add(5 * time.Second))
		if _, err := hub.Write(b); err != nil {
			t.Fatal(err)
		}
	}
	recv = func(wait time.Duration) msg {
		_ = hub.SetReadDeadline(time.Now().Add(wait))
		m, err := readMessage(hub)
		if err != nil {
			return nil
		}
		return m
	}
	return send, recv
}

// blockingHostname waits for a release file. It also gives up on its own (state
// dir gone, or ~20 s elapsed), so a failing test never leaves it spinning.
const blockingHostname = `i=0
while [ ! -f "$HP_TEST_STATE/release" ] && [ -d "$HP_TEST_STATE" ] && [ $i -lt 400 ]; do
  sleep 0.05
  i=$((i+1))
done
echo released
`

// releaseOnCleanup unblocks every stub before the state dir is removed.
func releaseOnCleanup(t *testing.T, state string) {
	t.Cleanup(func() { _ = os.WriteFile(filepath.Join(state, "release"), nil, 0o600) })
}

func TestASlowRequestDoesNotBlockTheNextOne(t *testing.T) {
	state := stubCommand(t, "hostname", blockingHostname)
	send, recv := serveOver(t)
	releaseOnCleanup(t, state)

	send(msg{"action": "exec", "command": "hostname", "timeout": 20, "request_id": "slow"})
	send(msg{"action": "read_file", "path": "/nonexistent/x", "request_id": "fast"})

	first := recv(3 * time.Second)
	if first == nil || str(first, "request_id") != "fast" {
		t.Fatalf("the read_file answer was held behind a running exec: got %v", first)
	}
	if err := os.WriteFile(filepath.Join(state, "release"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	second := recv(10 * time.Second)
	if second == nil || str(second, "request_id") != "slow" || !strings.Contains(str(second, "stdout"), "released") {
		t.Fatalf("the slow exec never completed: %v", second)
	}
}

// The pool is bounded: past maxConcurrentRequests the agent answers "busy" at
// once instead of queueing without limit or stalling the read loop.
func TestAFullWorkerPoolAnswersBusyImmediately(t *testing.T) {
	state := stubCommand(t, "hostname", blockingHostname)
	send, recv := serveOver(t)
	releaseOnCleanup(t, state)
	for i := 0; i < maxConcurrentRequests; i++ {
		send(msg{"action": "exec", "command": "hostname", "timeout": 20, "request_id": "busy-" + strconv.Itoa(i)})
	}
	send(msg{"action": "exec", "command": "hostname", "timeout": 20, "request_id": "overflow"})
	got := recv(3 * time.Second)
	if got == nil || str(got, "request_id") != "overflow" || !strings.Contains(str(got, "error"), "busy") {
		t.Fatalf("expected an immediate busy refusal for the overflow request, got %v", got)
	}
	if str(got, "for_action") != "exec" {
		t.Fatalf("the busy refusal must name the action it answers: %v", got)
	}
	_ = os.WriteFile(filepath.Join(state, "release"), nil, 0o600)
	for i := 0; i < maxConcurrentRequests; i++ {
		if recv(10*time.Second) == nil {
			t.Fatalf("worker %d never finished after release", i)
		}
	}
}
