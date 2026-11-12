package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #723: the hub's write_file (and write_config) could overwrite the agent's own
// state. /etc/homepilot/ is a write prefix on both grant types and the write
// path had no denylist, so writing agent.transport = {"tls":true,"pin":"sha256:zz"}
// bricked the agent (every later connect failed on the unparseable pin, with no
// channel left to fix it), writing agent.token / agent.id orphaned its
// credential, and writing agent.env could widen HP_AGENT_READ_PREFIXES for the
// next restart.
//
// Revert-check: drop the agent-state check from resolveWritePath and
// TestWritesToTheAgentsOwnStateAreRefused fails for every file; drop the pin
// validation in readTransportFile and TestAnUnusablePersistedPinFallsBackToEnv
// fails with a tlsConfig error.

func agentStateDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	conf := filepath.Join(dir, "homepilot")
	if err := os.MkdirAll(conf, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_CONFIG_DIR", conf)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", conf+"/")
	t.Setenv("HP_AGENT_TOKEN_FILE", filepath.Join(conf, "agent.token"))
	t.Setenv("HP_AGENT_ID_FILE", "")
	t.Setenv("HP_AGENT_TRANSPORT_FILE", "")
	t.Setenv("HP_AGENT_ENV_FILE", "")
	return conf
}

func TestWritesToTheAgentsOwnStateAreRefused(t *testing.T) {
	conf := agentStateDir(t)
	for _, name := range []string{"agent.transport", "agent.token", "agent.token.prev", "agent.id", "agent.env"} {
		path := filepath.Join(conf, name)
		if err := os.WriteFile(path, []byte("original"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := writeFile(path, `{"tls":true,"pin":"sha256:zz"}`); err == nil {
			t.Errorf("write_file overwrote the agent's own %s", name)
		}
		if _, err := writeConfig(true, path, "HP_AGENT_READ_PREFIXES=/", "0600"); err == nil {
			t.Errorf("write_config overwrote the agent's own %s", name)
		}
		if got, _ := os.ReadFile(path); string(got) != "original" {
			t.Errorf("%s was changed: %q", name, got)
		}
	}
	// Artifacts still own the rest of the directory.
	if err := writeFile(filepath.Join(conf, "some-artifact.conf"), "x=1\n"); err != nil {
		t.Fatalf("an ordinary file in the config dir must stay writable: %v", err)
	}
}

// The state files are found where the agent actually keeps them, not only by
// their default names: an id or transport file moved by env is protected too.
func TestRelocatedStateFilesAreProtected(t *testing.T) {
	conf := agentStateDir(t)
	idFile := filepath.Join(conf, "custom-id")
	transport := filepath.Join(conf, "custom-transport")
	t.Setenv("HP_AGENT_ID_FILE", idFile)
	t.Setenv("HP_AGENT_TRANSPORT_FILE", transport)
	for _, path := range []string{idFile, transport} {
		if err := writeFile(path, "x"); err == nil {
			t.Errorf("write_file wrote the relocated state file %s", path)
		}
	}
}

// A symlinked config directory is the same directory.
func TestStateFilesProtectedThroughASymlinkedDir(t *testing.T) {
	conf := agentStateDir(t)
	alias := filepath.Join(filepath.Dir(conf), "alias")
	if err := os.Symlink(conf, alias); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_WRITE_PREFIXES", conf+"/:"+alias+"/")
	if err := writeFile(filepath.Join(alias, "agent.transport"), "x"); err == nil {
		t.Fatal("write_file reached agent.transport through a symlinked directory")
	}
}

func TestAnUnusablePersistedPinFallsBackToEnv(t *testing.T) {
	dir := t.TempDir()
	transport := filepath.Join(dir, "agent.transport")
	if err := os.WriteFile(transport, []byte(`{"tls":true,"pin":"sha256:zz"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_TRANSPORT_FILE", transport)
	t.Setenv("HP_AGENT_TLS", "")
	t.Setenv("HP_AGENT_TLS_PIN", "")

	if _, ok := readTransportFile(transport); ok {
		t.Fatal("a transport file with an unparseable pin was accepted")
	}
	cfg := ConfigFromEnv()
	if cfg.TLS || cfg.TLSPin != "" {
		t.Fatalf("the unusable persisted pin overrode the env transport: tls=%v pin=%q", cfg.TLS, cfg.TLSPin)
	}
	if _, err := cfg.tlsConfig(); err != nil {
		t.Fatalf("the agent cannot build its transport: %v", err)
	}

	// And a good pin is still honoured.
	good := "sha256:" + strings.Repeat("ab", 32)
	if err := writeTransportFile(transport, persistedTransport{TLS: true, Pin: good}); err != nil {
		t.Fatal(err)
	}
	if cfg := ConfigFromEnv(); !cfg.TLS || cfg.TLSPin != good {
		t.Fatalf("a valid persisted transport was not applied: tls=%v pin=%q", cfg.TLS, cfg.TLSPin)
	}
}
