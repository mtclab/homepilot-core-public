package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #422: the agent must refuse to start when privileged mode is REQUESTED but the
// runtime cannot honour it. The old failure mode was silent: the installer wrote
// HP_AGENT_PRIVILEGED=true into a unit that ran as hp-agent under
// ProtectSystem=strict, and nothing said a word until a provisioning request
// failed on a remote host hours later.

func writableDir(t *testing.T) string {
	t.Helper()
	d := t.TempDir()
	real, err := filepath.EvalSymlinks(d)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

func readOnlyDir(t *testing.T) string {
	t.Helper()
	if os.Geteuid() == 0 {
		t.Skip("running as root: mode bits cannot make a directory unwritable")
	}
	d := writableDir(t)
	sub := filepath.Join(d, "locked")
	if err := os.Mkdir(sub, 0o555); err != nil {
		t.Fatal(err)
	}
	return sub
}

func TestPreflightPrivilegedNonRootRefuses(t *testing.T) {
	cfg := Config{Privileged: true}
	_, err := preflight(cfg, 1000, []string{writableDir(t)})
	if err == nil {
		t.Fatal("expected a privileged non-root agent to refuse to start")
	}
	for _, want := range []string{"HP_AGENT_PRIVILEGED", "euid 1000", "install-agent.sh --privileged"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("diagnostic must mention %q, got:\n%v", want, err)
		}
	}
}

func TestPreflightPrivilegedRootWithWritablePrefixesStarts(t *testing.T) {
	cfg := Config{Privileged: true}
	report, err := preflight(cfg, 0, []string{writableDir(t), writableDir(t)})
	if err != nil {
		t.Fatalf("expected a correctly installed privileged agent to start: %v", err)
	}
	if !strings.Contains(strings.Join(report, "\n"), "writable") {
		t.Errorf("report must state each prefix's status, got:\n%s", strings.Join(report, "\n"))
	}
}

// The exact shape of the shipped bug: root, but the write prefixes are not
// writable (a ProtectSystem=strict unit whose ReadWritePaths omits them).
func TestPreflightPrivilegedUnwritablePrefixRefuses(t *testing.T) {
	locked := readOnlyDir(t)
	ok := writableDir(t)
	cfg := Config{Privileged: true}
	_, err := preflight(cfg, 0, []string{ok, locked})
	if err == nil {
		t.Fatal("expected an unwritable write prefix to refuse the start")
	}
	if !strings.Contains(err.Error(), locked) {
		t.Errorf("diagnostic must name the offending prefix %q, got:\n%v", locked, err)
	}
	if !strings.Contains(err.Error(), "ReadWritePaths") {
		t.Errorf("diagnostic must name the systemd directive to fix, got:\n%v", err)
	}
}

// An absent prefix is reported, not fatal: /etc/nginx legitimately does not
// exist until nginx is installed, and under a strict unit the agent could not
// create it anyway.
func TestPreflightAbsentPrefixIsNotFatal(t *testing.T) {
	absent := filepath.Join(writableDir(t), "not-there")
	report, err := preflight(Config{Privileged: true}, 0, []string{absent})
	if err != nil {
		t.Fatalf("an absent prefix must not block startup: %v", err)
	}
	if !strings.Contains(strings.Join(report, "\n"), "absent") {
		t.Errorf("report must flag the absent prefix, got:\n%s", strings.Join(report, "\n"))
	}
}

// Unprivileged mode warns but never refuses: an unwritable prefix costs it a
// file write, not a contract it advertised to the hub.
func TestPreflightUnprivilegedNeverFatal(t *testing.T) {
	locked := readOnlyDir(t)
	report, err := preflight(Config{Privileged: false}, 1000, []string{locked})
	if err != nil {
		t.Fatalf("unprivileged startup must not fail: %v", err)
	}
	joined := strings.Join(report, "\n")
	if !strings.Contains(joined, "WARNING") {
		t.Errorf("report must warn about the unwritable prefix, got:\n%s", joined)
	}
	if !strings.Contains(joined, "privileged mode: OFF") {
		t.Errorf("report must state the mode, got:\n%s", joined)
	}
}

func TestPreflightReportsThePackageGrant(t *testing.T) {
	d := writableDir(t)
	granted, err := preflight(Config{Privileged: true, AllowPackageInstall: true}, 0, []string{d})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(granted, "\n"), "package installs: granted") {
		t.Errorf("report must state the package grant, got:\n%s", strings.Join(granted, "\n"))
	}
	withheld, err := preflight(Config{Privileged: true}, 0, []string{d})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(withheld, "\n"), "package installs: NOT granted") {
		t.Errorf("report must state the withheld grant, got:\n%s", strings.Join(withheld, "\n"))
	}
}

// probeWritePrefix must detect unwritability by ATTEMPTING a write: a read-only
// bind mount leaves the mode bits looking perfectly writable.
func TestProbeWritePrefixAttemptsARealWrite(t *testing.T) {
	d := writableDir(t)
	if s := probeWritePrefix(d); s.State != prefixWritable {
		t.Fatalf("expected %s writable, got %v (%v)", d, s.State, s.Err)
	}
	// The probe must clean up after itself.
	entries, err := os.ReadDir(d)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".hp-preflight-") {
			t.Fatalf("probe left %s behind", e.Name())
		}
	}
	file := filepath.Join(d, "a-file")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if s := probeWritePrefix(file); s.State != prefixUnwritable {
		t.Fatalf("a non-directory prefix must not count as writable, got %v", s.State)
	}
}
