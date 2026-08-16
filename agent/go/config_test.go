package main

import (
	"os"
	"path/filepath"
	"testing"
)

// #377: TLS verification must be ON by default. With TLS enabled but no CA and
// no HP_AGENT_TLS_INSECURE, the returned config must NOT skip verification and
// must leave RootCAs nil so Go falls back to the system trust store.
func TestTLSConfigVerifiesByDefault(t *testing.T) {
	t.Setenv("HP_AGENT_TLS_INSECURE", "")
	c := Config{TLS: true}
	cfg, err := c.tlsConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil tls config when TLS enabled")
	}
	if cfg.InsecureSkipVerify {
		t.Fatal("InsecureSkipVerify must be false by default (no CA, no HP_AGENT_TLS_INSECURE)")
	}
	if cfg.RootCAs != nil {
		t.Fatal("RootCAs must be nil so the system trust store is used")
	}
}

// #377: the only way to disable verification is the explicit escape hatch.
func TestTLSConfigInsecureEscapeHatch(t *testing.T) {
	t.Setenv("HP_AGENT_TLS_INSECURE", "1")
	c := Config{TLS: true}
	cfg, err := c.tlsConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil tls config when TLS enabled")
	}
	if !cfg.InsecureSkipVerify {
		t.Fatal("HP_AGENT_TLS_INSECURE=1 must set InsecureSkipVerify=true")
	}
}

// The agent id MUST be stable across restarts. Before the fix an agent with no
// HP_AGENT_ID minted a brand-new UUID on every process start, which orphaned the
// per-agent credential issued to the previous id: the agent then presented a
// token the hub could not match for the claimed id and was banned after
// MAX_AUTH_FAILURES.
//
// Revert-check: drop the persist (or the read-back) in resolveAgentID and the
// second resolve returns a DIFFERENT id -> this fails.
func TestAgentIDIsStableAcrossRestarts(t *testing.T) {
	idFile := filepath.Join(t.TempDir(), "agent.id")

	first := resolveAgentID("", idFile)
	if first == "" {
		t.Fatal("expected a generated agent id")
	}
	b, err := os.ReadFile(idFile)
	if err != nil {
		t.Fatalf("agent id was not persisted to %s: %v", idFile, err)
	}
	if string(b) != first {
		t.Fatalf("persisted id %q != returned id %q", string(b), first)
	}
	st, err := os.Stat(idFile)
	if err != nil {
		t.Fatalf("stat id file: %v", err)
	}
	if perm := st.Mode().Perm(); perm != 0o600 {
		t.Fatalf("id file must be 0600, got %04o", perm)
	}

	// A second start (fresh process, same config) must reuse the SAME id.
	if second := resolveAgentID("", idFile); second != first {
		t.Fatalf("agent id changed across restarts: %q -> %q", first, second)
	}
}

// An explicit HP_AGENT_ID always wins over the persisted file.
func TestAgentIDEnvWinsOverFile(t *testing.T) {
	idFile := filepath.Join(t.TempDir(), "agent.id")
	if err := os.WriteFile(idFile, []byte("persisted-id\n"), 0o600); err != nil {
		t.Fatalf("write id file: %v", err)
	}
	if got := resolveAgentID("explicit-id", idFile); got != "explicit-id" {
		t.Fatalf("expected HP_AGENT_ID to win, got %q", got)
	}
	// And a whitespace-padded persisted id is trimmed, not used verbatim.
	if got := resolveAgentID("", idFile); got != "persisted-id" {
		t.Fatalf("expected trimmed persisted id, got %q", got)
	}
}

// An unwritable id path must not crash or block startup: the agent runs with the
// in-memory id (loudly warned) instead of refusing to start.
func TestAgentIDUnwritablePathFallsBack(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "blocker")
	if err := os.WriteFile(blocker, []byte("not a directory"), 0o600); err != nil {
		t.Fatalf("write blocker: %v", err)
	}
	// Parent of the id file is a regular file -> MkdirAll/CreateTemp must fail.
	id := resolveAgentID("", filepath.Join(blocker, "agent.id"))
	if id == "" {
		t.Fatal("expected a usable in-memory agent id when the path is unwritable")
	}
}

// The id file lives beside the token file by default, is overridable by
// HP_AGENT_ID_FILE, and falls back to the packaged path when neither is set.
func TestAgentIDFilePathDerivation(t *testing.T) {
	if got := agentIDFilePath("/custom/id", "/etc/homepilot/agent.token"); got != "/custom/id" {
		t.Fatalf("HP_AGENT_ID_FILE must win, got %q", got)
	}
	if got := agentIDFilePath("", "/etc/homepilot/agent.token"); got != "/etc/homepilot/agent.id" {
		t.Fatalf("expected id file beside the token file, got %q", got)
	}
	if got := agentIDFilePath("", ""); got != defaultAgentIDFile {
		t.Fatalf("expected %q, got %q", defaultAgentIDFile, got)
	}
}

// ConfigFromEnv must carry both the id-file path and the raw env token (the
// enrollment credential the self-heal falls back to).
func TestConfigFromEnvCarriesIDFileAndEnvToken(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HP_AGENT_TOKEN_FILE", filepath.Join(dir, "agent.token"))
	t.Setenv("HP_AGENT_AUTH_TOKEN", "enrollment-token")
	t.Setenv("HP_AGENT_ID_FILE", "")
	cfg := ConfigFromEnv()
	if want := filepath.Join(dir, "agent.id"); cfg.AgentIDFile != want {
		t.Fatalf("AgentIDFile = %q, want %q", cfg.AgentIDFile, want)
	}
	if cfg.EnvAuthToken != "enrollment-token" {
		t.Fatalf("EnvAuthToken = %q, want %q", cfg.EnvAuthToken, "enrollment-token")
	}
}
