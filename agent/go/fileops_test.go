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
