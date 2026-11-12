package main

import (
	"bytes"
	"log"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The hub's two enrolment refusals, verbatim (agent_hub/server.py
// ENROLMENT_WINDOW_CLOSED_ERROR and HOST_HOLDS_CREDENTIAL_ERROR).
// tests/test_reinstall_stale_credential.py holds the hub side to the same prefix.
const (
	hubWindowClosed = "enrolment refused: this hub is not accepting new hosts right now. Ask an " +
		"operator to open the enrolment window (or enrol with a bootstrap token)."
	hubHostHoldsCredential = "enrolment refused: this hostname already holds its own agent credential. " +
		"Reconnect with that credential, or ask an operator to revoke it (or enrol " +
		"with a bootstrap token)."
)

// captureLog collects the agent's log output so a test can prove no token is in it.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })
	return &buf
}

// attempt runs one register() against a hub that answers resp, and returns the
// credential the agent presented.
func attempt(t *testing.T, a *Agent, resp msg) (string, error) {
	t.Helper()
	c, s := net.Pipe()
	defer c.Close()
	defer s.Close()
	a.conn = c
	sent := serveOnce(t, s, resp)
	err := a.register()
	m := <-sent
	if m == nil {
		t.Fatal("the agent sent no register frame")
	}
	tok, _ := m["auth_token"].(string)
	return tok, err
}

func assertNoTokenLogged(t *testing.T, logs *bytes.Buffer, tokens ...string) {
	t.Helper()
	for _, tok := range tokens {
		if strings.Contains(logs.String(), tok) {
			t.Fatalf("a credential reached the log: %q", logs.String())
		}
	}
}

func TestEnrolmentRefusalIsRecognised(t *testing.T) {
	for _, e := range []string{hubWindowClosed, hubHostHoldsCredential} {
		if !isEnrolmentRefusal(e) {
			t.Fatalf("%q must be recognised as an enrolment refusal", e)
		}
		if isAuthError(e) {
			t.Fatalf("%q must stay out of the auth-error rotation", e)
		}
	}
	for _, e := range []string{"invalid auth_token", "identity already claimed by a live connection"} {
		if isEnrolmentRefusal(e) {
			t.Fatalf("%q is not an enrolment refusal", e)
		}
	}
}

// #815 on prod: a guest re-installed with a fresh bootstrap token still held the
// agent.token an old (#348-era) hub handed back - the shared fleet token. The hub
// refused it for want of an enrolment window, the agent never tried the bootstrap
// token it had been given, and the install timed out.
//
// Revert-check: drop the isEnrolmentRefusal case in register() and attempt 2
// presents the refused stored token again -> this fails.
func TestStoredCredentialRefusedForEnrolmentFallsBackToEnvToken(t *testing.T) {
	logs := captureLog(t)
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	const stale, bootstrap, minted = "stale-shared-token-0001", "fresh-bootstrap-0002", "minted-credential-0003"
	if err := os.WriteFile(tokenFile, []byte(stale), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{
		cfg:            Config{AuthToken: stale, AuthTokenFile: tokenFile, EnvAuthToken: bootstrap},
		agentID:        "agent-815",
		hasPerAgent:    true,
		usingFileToken: true,
	}

	tok, err := attempt(t, a, msg{"error": hubWindowClosed})
	if err == nil || tok != stale {
		t.Fatalf("attempt 1 presents the stored credential and is refused, got %q err=%v", tok, err)
	}
	tok, err = attempt(t, a, msg{"action": "register_ack", "agent_id": "agent-815", "auth_token": minted})
	if err != nil {
		t.Fatalf("enrolment with the configured token failed: %v", err)
	}
	if tok != bootstrap {
		t.Fatalf("attempt 2 must present the configured enrolment token, presented %q", tok)
	}
	got, err := os.ReadFile(tokenFile)
	if err != nil || string(got) != minted {
		t.Fatalf("the minted credential must be persisted, got %q (err %v)", got, err)
	}
	if fi, _ := os.Stat(tokenFile); fi.Mode().Perm() != 0o600 {
		t.Fatalf("token file mode %o, want 0600", fi.Mode().Perm())
	}
	assertNoTokenLogged(t, logs, stale, bootstrap, minted)
}

// The fallback is made once. Two credentials that are both refused - the stored
// one for enrolment, the configured one as unknown - must not be alternated for
// ever: after the single switch and the auth rotation back, an enrolment refusal
// leaves the agent where it is.
func TestEnrolmentFallbackNeverLoops(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	const stored, env = "stored-token-a", "env-token-b"
	if err := os.WriteFile(tokenFile, []byte(stored), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{
		cfg:            Config{AuthToken: stored, AuthTokenFile: tokenFile, EnvAuthToken: env},
		agentID:        "agent-loop",
		hasPerAgent:    true,
		usingFileToken: true,
	}
	want := []string{stored, env, stored, stored, stored}
	replies := []msg{
		{"error": hubWindowClosed},
		{"error": "invalid auth_token"},
		{"error": hubWindowClosed},
		{"error": hubWindowClosed},
		{"error": hubWindowClosed},
	}
	for i, r := range replies {
		tok, err := attempt(t, a, r)
		if err == nil {
			t.Fatalf("attempt %d must fail", i+1)
		}
		if tok != want[i] {
			t.Fatalf("attempt %d presented %q, want %q", i+1, tok, want[i])
		}
	}
}

// The other direction, and the #789 guard on a reinstall: the installer moved a
// HEALTHY host's credential aside to agent.token.prev and brought a token the hub
// refuses (here the shared token, refused because the host holds its own
// credential). The agent must present the kept credential once, and put it back
// into agent.token when the hub accepts it, so the next restart is not refused.
//
// Revert-check: return early from fallBackAfterEnrolmentRefusal when not using
// the file token (or drop the agent.token.prev lookup in storedCredential) and
// attempt 2 presents the refused env token again -> this fails.
func TestRefusedFreshTokenFallsBackToThePreviousInstallsCredential(t *testing.T) {
	logs := captureLog(t)
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	const kept, shared = "kept-per-agent-credential-0004", "shared-fleet-token-0005"
	if err := os.WriteFile(prevTokenFilePath(tokenFile), []byte(kept+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{
		cfg:     Config{AuthToken: shared, AuthTokenFile: tokenFile, EnvAuthToken: shared},
		agentID: "agent-healthy",
	}

	tok, err := attempt(t, a, msg{"error": hubHostHoldsCredential})
	if err == nil || tok != shared {
		t.Fatalf("attempt 1 presents the configured token and is refused, got %q err=%v", tok, err)
	}
	// Accepted as the per-agent credential: no new token in the ack.
	tok, err = attempt(t, a, msg{"action": "register_ack", "agent_id": "agent-healthy"})
	if err != nil {
		t.Fatalf("the kept credential was refused: %v", err)
	}
	if tok != kept {
		t.Fatalf("attempt 2 must present the previous install's credential, presented %q", tok)
	}
	got, err := os.ReadFile(tokenFile)
	if err != nil || string(got) != kept {
		t.Fatalf("the accepted credential must be back in agent.token, got %q (err %v)", got, err)
	}
	if fi, _ := os.Stat(tokenFile); fi.Mode().Perm() != 0o600 {
		t.Fatalf("token file mode %o, want 0600", fi.Mode().Perm())
	}
	if prev, _ := os.ReadFile(prevTokenFilePath(tokenFile)); strings.TrimSpace(string(prev)) != kept {
		t.Fatal("the previous copy must be kept, never deleted")
	}
	assertNoTokenLogged(t, logs, kept, shared)
}

// An expired or already-used bootstrap token is refused as unknown ("invalid
// auth_token"), which the auth rotation handles; with agent.token moved aside it
// must still find the kept credential.
func TestAuthRotationFindsThePreviousInstallsCredential(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(prevTokenFilePath(tokenFile), []byte("kept-0006"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{cfg: Config{AuthToken: "spent-0007", AuthTokenFile: tokenFile, EnvAuthToken: "spent-0007"}}
	a.rotateTokenSource()
	if a.cfg.AuthToken != "kept-0006" || !a.usingFileToken {
		t.Fatalf("rotation must reach agent.token.prev when agent.token is absent, got %q", a.cfg.AuthToken)
	}
}
