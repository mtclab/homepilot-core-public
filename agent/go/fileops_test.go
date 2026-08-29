package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// realTempDir returns a symlink-resolved temp dir so that path comparisons
// against EvalSymlinks-resolved results are stable (e.g. /tmp -> /private/tmp).
func realTempDir(t *testing.T) string {
	t.Helper()
	d, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return d
}

// (a) #378: /etc is an allowed read prefix, but /etc/shadow is on the denylist
// and must always be blocked.
func TestReadDeniesShadowInsideAllowedPrefix(t *testing.T) {
	_, err := readFile("/etc/shadow")
	if err == nil {
		t.Fatal("expected /etc/shadow read to be denied even though /etc is allowed")
	}
	// Must be denied by the denylist, not merely by filesystem permissions —
	// otherwise the guard has no teeth for a privileged agent.
	if !strings.Contains(err.Error(), "forbidden") {
		t.Fatalf("expected denylist rejection, got: %v", err)
	}
}

// (b) #378: a readable file outside every allowed prefix is denied by the
// allowlist (not merely by non-existence).
func TestReadDeniesOutsideAllowedPrefixes(t *testing.T) {
	dir := realTempDir(t)
	f := filepath.Join(dir, "outside.txt")
	if err := os.WriteFile(f, []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readFile(f); err == nil {
		t.Fatalf("expected read outside allowed prefixes to be denied: %s", f)
	}
}

// (c) #378: a file under a permitted prefix reads successfully.
func TestReadAllowedUnderPermittedPrefix(t *testing.T) {
	dir := realTempDir(t)
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)
	f := filepath.Join(dir, "config.conf")
	if err := os.WriteFile(f, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readFile(f)
	if err != nil {
		t.Fatalf("expected allowed read to succeed: %v", err)
	}
	if got != "hello" {
		t.Fatalf("got %q, want %q", got, "hello")
	}
}

// (d) #378: a write whose parent is a symlink pointing outside the allowlist is
// rejected (symlink defense), and nothing lands at the escaped location.
func TestWriteRejectsSymlinkedParentEscapingAllowlist(t *testing.T) {
	allowed := realTempDir(t)
	outside := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	link := filepath.Join(allowed, "sub")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(link, "evil.txt")
	if err := writeFile(target, "x"); err == nil {
		t.Fatal("expected write through a symlinked parent escaping the allowlist to be rejected")
	}
	if _, err := os.Stat(filepath.Join(outside, "evil.txt")); err == nil {
		t.Fatal("file must not have landed outside the allowlist")
	}
}

// (e) #378: a normal allowed write lands atomically and honours the requested
// mode (here 0600 -> not group/world accessible).
func TestWriteAtomicRespectsMode(t *testing.T) {
	allowed := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(allowed, "cfg.txt")
	if err := writeFileMode(target, "data", 0o600); err != nil {
		t.Fatalf("expected allowed write to succeed: %v", err)
	}
	b, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "data" {
		t.Fatalf("got %q, want %q", string(b), "data")
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("file must not be group/world accessible, got mode %o", info.Mode().Perm())
	}
}

// The agent must never hand back its OWN credentials (review #648).
//
// /etc/homepilot/agent.env is what scripts/install-agent.sh writes. It carries
// HP_AGENT_AUTH_TOKEN - the SHARED FLEET ENROLMENT TOKEN - plus the hub's
// address and its certificate pin. The control plane refuses to serve that
// token over MCP by name (GET /agents/token and the installer one-liner are
// both excluded: "a credential that provisions machines must not appear in an
// MCP transcript"); read_file_on_guest, a READ-tier tool, read it straight off
// the host instead. Verified live on dev at 3.6.14.
//
// The previous guard covered only the file HP_AGENT_TOKEN_FILE names - the
// DURABLE per-agent credential - and missed the shared one sitting next to it.
//
// TEETH: remove the agentSecretBases branch from isDenied and both files below
// come back as content instead of an error.
func TestReadDeniesTheAgentsOwnCredentialFiles(t *testing.T) {
	dir := realTempDir(t)
	conf := filepath.Join(dir, "homepilot")
	if err := os.MkdirAll(conf, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_CONFIG_DIR", conf)
	// The config dir is inside a temp tree, so make it readable at all -
	// otherwise the allowlist would refuse it and the test would pass vacuously.
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)
	// Deliberately NO HP_AGENT_TOKEN_FILE: a bootstrap-enrolled agent has none
	// yet, and agent.env holds the shared token either way.
	t.Setenv("HP_AGENT_TOKEN_FILE", "")

	for _, name := range []string{"agent.env", "agent.token"} {
		path := filepath.Join(conf, name)
		secret := []byte("HP_AGENT_AUTH_TOKEN=fleet-enrolment-secret\n")
		if err := os.WriteFile(path, secret, 0o600); err != nil {
			t.Fatal(err)
		}
		out, err := readFile(path)
		if err == nil {
			t.Fatalf("%s was served to the caller: %q", name, out)
		}
		if !strings.Contains(err.Error(), "forbidden") {
			t.Fatalf("%s was refused for the wrong reason (want a denylist rejection): %v", name, err)
		}
	}
}

// Guard the guard: the denial is by NAME inside the agent's configuration
// directory, not a ban on the directory. /etc/homepilot is a granted WRITE
// prefix, so artifacts legitimately put files there and those must stay
// readable - a blanket ban would look like a stronger fix and break the product.
func TestTheAgentConfigDirIsNotDeniedWholesale(t *testing.T) {
	dir := realTempDir(t)
	conf := filepath.Join(dir, "homepilot")
	if err := os.MkdirAll(conf, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_CONFIG_DIR", conf)
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)

	path := filepath.Join(conf, "some-artifact.conf")
	if err := os.WriteFile(path, []byte("listen = 8080\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readFile(path)
	if err != nil {
		t.Fatalf("an ordinary file in the agent config dir was refused: %v", err)
	}
	if !strings.Contains(got, "listen = 8080") {
		t.Fatalf("unexpected content: %q", got)
	}
}
