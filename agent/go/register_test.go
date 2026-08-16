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

// serveOnce reads one frame from the pipe's server end and replies with resp.
// Returns the frame the agent sent.
func serveOnce(t *testing.T, server net.Conn, resp msg) chan msg {
	t.Helper()
	got := make(chan msg, 1)
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		m, err := readMessage(server)
		if err != nil {
			got <- nil
			return
		}
		b, _ := encodeMessage(resp)
		_, _ = server.Write(b)
		got <- m
	}()
	return got
}

// Self-heal: when the hub rejects the STORED per-agent credential, the agent must
// fall back to the configured enrollment token for the next attempt and adopt +
// persist the freshly minted credential. Without this an agent whose stored
// credential the hub cannot match (rebuilt hub DB, revoked credential, drifted
// id) retries the dead token forever and is banned after MAX_AUTH_FAILURES.
//
// Revert-check: remove the rotateTokenSource() call in register() and the second
// attempt still presents the rejected file token -> this fails.
func TestRegisterFallsBackToEnvTokenAfterAuthRejection(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(tokenFile, []byte("stale-per-agent-token"), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}

	a := &Agent{
		cfg: Config{
			AuthToken:     "stale-per-agent-token",
			AuthTokenFile: tokenFile,
			EnvAuthToken:  "shared-enrollment-token",
		},
		agentID:        "agent-stable",
		hasPerAgent:    true,
		usingFileToken: true,
	}

	// Attempt 1: the hub rejects the stored credential.
	c1, s1 := net.Pipe()
	defer c1.Close()
	defer s1.Close()
	a.conn = c1
	sent1 := serveOnce(t, s1, msg{"error": "invalid auth_token"})
	if err := a.register(); err == nil {
		t.Fatal("expected registration to fail on a rejected credential")
	}
	if m := <-sent1; m == nil || m["auth_token"] != "stale-per-agent-token" {
		t.Fatalf("attempt 1 should present the stored token, got %v", m)
	}
	if a.cfg.AuthToken != "shared-enrollment-token" {
		t.Fatalf("after rejection the agent must fall back to the enrollment token, got %q", a.cfg.AuthToken)
	}
	if a.hasPerAgent {
		t.Fatal("replay negotiation must be off while using the enrollment token")
	}

	// Attempt 2: re-enrollment succeeds and hands back a fresh credential.
	c2, s2 := net.Pipe()
	defer c2.Close()
	defer s2.Close()
	a.conn = c2
	sent2 := serveOnce(t, s2, msg{
		"action":     "register_ack",
		"agent_id":   "agent-stable",
		"auth_token": "fresh-per-agent-token",
	})
	if err := a.register(); err != nil {
		t.Fatalf("re-enrollment failed: %v", err)
	}
	m2 := <-sent2
	if m2 == nil || m2["auth_token"] != "shared-enrollment-token" {
		t.Fatalf("attempt 2 must present the enrollment token, got %v", m2)
	}
	b, err := os.ReadFile(tokenFile)
	if err != nil || string(b) != "fresh-per-agent-token" {
		t.Fatalf("re-enrollment must overwrite the stored credential, got %q (err %v)", string(b), err)
	}
	if !a.usingFileToken || !a.hasPerAgent {
		t.Fatal("after re-enrollment the agent holds a per-agent credential again")
	}
}

// The self-heal only ALTERNATES between the two credential sources, so a hub that
// rejects both cannot push the agent into a tight token-guessing loop.
func TestTokenSourceOnlyAlternates(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(tokenFile, []byte("file-token"), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}
	a := &Agent{cfg: Config{
		AuthToken:     "file-token",
		AuthTokenFile: tokenFile,
		EnvAuthToken:  "env-token",
	}, usingFileToken: true}

	a.rotateTokenSource()
	if a.cfg.AuthToken != "env-token" {
		t.Fatalf("first rotation -> env token, got %q", a.cfg.AuthToken)
	}
	a.rotateTokenSource()
	if a.cfg.AuthToken != "file-token" {
		t.Fatalf("second rotation -> back to the file token, got %q", a.cfg.AuthToken)
	}
	a.rotateTokenSource()
	if a.cfg.AuthToken != "env-token" {
		t.Fatalf("third rotation -> env token again, got %q", a.cfg.AuthToken)
	}

	// With no env token there is nothing to alternate with: stay put.
	b := &Agent{cfg: Config{AuthToken: "file-token", AuthTokenFile: tokenFile}, usingFileToken: true}
	b.rotateTokenSource()
	if b.cfg.AuthToken != "file-token" {
		t.Fatalf("without an enrollment token the agent keeps its token, got %q", b.cfg.AuthToken)
	}
}

// Only credential rejections rotate the token; a duplicate-identity or freshness
// rejection must leave the credential alone (changing it would not help).
func TestNonAuthErrorsDoNotRotateToken(t *testing.T) {
	for _, e := range []string{
		"identity already claimed by a live connection",
		"must register first",
		"register rejected: stale ts",
	} {
		if isAuthError(e) {
			t.Fatalf("%q must not be treated as an auth failure", e)
		}
	}
	if !isAuthError("invalid auth_token") {
		t.Fatal("the hub's credential rejection must be treated as an auth failure")
	}
}
