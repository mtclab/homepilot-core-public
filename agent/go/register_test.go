package main

import (
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// #362 slice 2: the register frame must advertise the protocol version so the
// hub can negotiate per-agent credentials. Revert-check: drop `"v":
// protocolVersion` from the register frame in main.go and this fails.
func TestRegisterFrameIncludesVersion(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()

	a := &Agent{cfg: Config{}, agentID: "agent-x"}
	a.conn = client

	type framed struct {
		m   msg
		err error
	}
	got := make(chan framed, 1)
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		m, err := readMessage(server)
		if err == nil {
			// Reply with a minimal register_ack so register() returns.
			ack, _ := encodeMessage(msg{"action": "register_ack", "agent_id": "agent-x"})
			_, _ = server.Write(ack)
		}
		got <- framed{m: m, err: err}
	}()

	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}

	res := <-got
	if res.err != nil {
		t.Fatalf("server read failed: %v", res.err)
	}
	if res.m["action"] != "register" {
		t.Fatalf("expected register frame, got %v", res.m["action"])
	}
	v, ok := res.m["v"]
	if !ok {
		t.Fatal("register frame missing protocol version field \"v\"")
	}
	// JSON numbers decode to float64.
	if vf, ok := v.(float64); !ok || int(vf) != protocolVersion {
		t.Fatalf("expected v=%d, got %v", protocolVersion, v)
	}
}

// The agent must adopt and persist a per-agent token handed back in
// register_ack, distinct from its env/bootstrap token. Revert-check: remove the
// durable-token handback block in main.go's register() and this fails.
func TestAgentAdoptsHandedBackPerAgentToken(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")

	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()

	a := &Agent{
		cfg:     Config{AuthToken: "shared-fleet-token", AuthTokenFile: tokenFile},
		agentID: "agent-y",
	}
	a.conn = client

	const perAgent = "minted-per-agent-token-abcdef"
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := readMessage(server); err != nil {
			return
		}
		ack, _ := encodeMessage(msg{
			"action":     "register_ack",
			"agent_id":   "agent-y",
			"auth_token": perAgent,
		})
		_, _ = server.Write(ack)
	}()

	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}

	if a.cfg.AuthToken != perAgent {
		t.Fatalf("expected adopted token %q, got %q", perAgent, a.cfg.AuthToken)
	}
	b, err := os.ReadFile(tokenFile)
	if err != nil {
		t.Fatalf("token file not written: %v", err)
	}
	if string(b) != perAgent {
		t.Fatalf("persisted token %q != handed-back token %q", string(b), perAgent)
	}
}

// resolveToken must prefer the persisted durable-credential file over the env
// token (which may be a one-time bootstrap token already consumed by the hub).
func TestResolveTokenPrefersFile(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(tokenFile, []byte("  durable-file-token\n"), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}
	if got := resolveToken(tokenFile, "env-token"); got != "durable-file-token" {
		t.Fatalf("expected file token to win, got %q", got)
	}
	// Falls back to env when the file is missing/empty.
	if got := resolveToken(filepath.Join(dir, "missing"), "env-token"); got != "env-token" {
		t.Fatalf("expected env fallback, got %q", got)
	}
}
