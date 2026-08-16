package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRunner records every command it is asked to run and returns a canned
// response keyed by the exact command string. Unmatched commands return exit 0
// with empty output. This is the injectable exec seam standing in for the
// allowlist-checked Executor so the idempotency decisions can be tested without
// touching the host.
type fakeResp struct {
	code int
	out  string
	err  string
}

type fakeRunner struct {
	resp  map[string]fakeResp
	calls []string
}

func newFakeRunner() *fakeRunner {
	return &fakeRunner{resp: map[string]fakeResp{}}
}

func (f *fakeRunner) run(command string, _ int) (int, string, string) {
	f.calls = append(f.calls, command)
	if r, ok := f.resp[command]; ok {
		return r.code, r.out, r.err
	}
	return 0, "", ""
}

func (f *fakeRunner) called(substr string) bool {
	for _, c := range f.calls {
		if strings.Contains(c, substr) {
			return true
		}
	}
	return false
}

// --- install_package -------------------------------------------------------

// Idempotency teeth: an already-installed package must return changed:false and
// must NOT run apt-get. Reverting the dpkg probe (always install) fails here.
func TestInstallPackageAlreadyInstalled(t *testing.T) {
	f := newFakeRunner()
	f.resp["dpkg -s nginx"] = fakeResp{code: 0, out: "Status: install ok installed\n"}
	res, err := installPackage(f.run, true, "nginx")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Changed {
		t.Fatal("expected changed:false for an already-installed package")
	}
	if f.called("apt-get") {
		t.Fatal("apt-get must not run when the package is already installed")
	}
}

// A not-installed package installs via the allowlisted apt-get form and reports
// changed:true.
func TestInstallPackageNotInstalledInstalls(t *testing.T) {
	f := newFakeRunner()
	f.resp["dpkg -s nginx"] = fakeResp{code: 1, out: ""}
	f.resp["apt-get install -y nginx"] = fakeResp{code: 0}
	res, err := installPackage(f.run, true, "nginx")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Changed {
		t.Fatal("expected changed:true when the package was installed")
	}
	if !f.called("apt-get install -y nginx") {
		t.Fatal("expected the allowlisted apt-get install form to run")
	}
}

// Privileged gate teeth: a non-privileged agent must be refused.
func TestInstallPackageRequiresPrivileged(t *testing.T) {
	f := newFakeRunner()
	if _, err := installPackage(f.run, false, "nginx"); err == nil {
		t.Fatal("expected install_package to require privileged mode")
	}
	if len(f.calls) != 0 {
		t.Fatal("no command should run when not privileged")
	}
}

// Injection teeth: a name with a metacharacter is rejected before any command
// is built.
func TestInstallPackageRejectsBadName(t *testing.T) {
	f := newFakeRunner()
	if _, err := installPackage(f.run, true, "nginx; rm -rf /"); err == nil {
		t.Fatal("expected an invalid package name to be rejected")
	}
	if len(f.calls) != 0 {
		t.Fatal("no command should run for an invalid name")
	}
}

// --- manage_service --------------------------------------------------------

// Idempotency: started on an already-active unit is a no-op.
func TestManageServiceStartedAlreadyActive(t *testing.T) {
	f := newFakeRunner()
	f.resp["systemctl is-active nginx"] = fakeResp{code: 0, out: "active\n"}
	res, err := manageService(f.run, true, "nginx", "started")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Changed {
		t.Fatal("expected changed:false when already active")
	}
	if f.called("systemctl start") {
		t.Fatal("systemctl start must not run when already active")
	}
}

// started on an inactive unit acts.
func TestManageServiceStartedInactiveStarts(t *testing.T) {
	f := newFakeRunner()
	f.resp["systemctl is-active nginx"] = fakeResp{code: 3, out: "inactive\n"}
	f.resp["systemctl start nginx"] = fakeResp{code: 0}
	res, err := manageService(f.run, true, "nginx", "started")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Changed {
		t.Fatal("expected changed:true when starting an inactive unit")
	}
	if !f.called("systemctl start nginx") {
		t.Fatal("expected the allowlisted systemctl start form to run")
	}
}

// restarted always acts, even when the unit is already active (a restart has no
// idempotent no-op). Reverting this to a state check fails here.
func TestManageServiceRestartedAlwaysActs(t *testing.T) {
	f := newFakeRunner()
	f.resp["systemctl is-active nginx"] = fakeResp{code: 0, out: "active\n"}
	f.resp["systemctl restart nginx"] = fakeResp{code: 0}
	res, err := manageService(f.run, true, "nginx", "restarted")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Changed {
		t.Fatal("expected restarted to always report changed:true")
	}
	if !f.called("systemctl restart nginx") {
		t.Fatal("expected systemctl restart to run")
	}
}

func TestManageServiceRequiresPrivileged(t *testing.T) {
	f := newFakeRunner()
	if _, err := manageService(f.run, false, "nginx", "started"); err == nil {
		t.Fatal("expected manage_service to require privileged mode")
	}
	if len(f.calls) != 0 {
		t.Fatal("no command should run when not privileged")
	}
}

func TestManageServiceRejectsBadState(t *testing.T) {
	f := newFakeRunner()
	if _, err := manageService(f.run, true, "nginx", "frobnicate"); err == nil {
		t.Fatal("expected an invalid service state to be rejected")
	}
	if len(f.calls) != 0 {
		t.Fatal("no command should run for an invalid state")
	}
}

func TestManageServiceRejectsBadName(t *testing.T) {
	f := newFakeRunner()
	if _, err := manageService(f.run, true, "ngi nx", "started"); err == nil {
		t.Fatal("expected an invalid service name to be rejected")
	}
	if len(f.calls) != 0 {
		t.Fatal("no command should run for an invalid name")
	}
}

// --- write_config ----------------------------------------------------------

// Idempotency: identical on-disk content skips the write (changed:false).
func TestWriteConfigIdenticalContent(t *testing.T) {
	allowed := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(allowed, "app.conf")
	if err := os.WriteFile(target, []byte("key = value\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := writeConfig(true, target, "key = value\n", "0644")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Changed {
		t.Fatal("expected changed:false for byte-identical content")
	}
}

// Different content writes atomically at the requested mode (changed:true).
func TestWriteConfigDifferentContentWrites(t *testing.T) {
	allowed := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(allowed, "app.conf")
	if err := os.WriteFile(target, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	res, err := writeConfig(true, target, "new = 1\n", "0600")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !res.Changed {
		t.Fatal("expected changed:true when content differs")
	}
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "new = 1\n" {
		t.Fatalf("got %q, want %q", string(got), "new = 1\n")
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("expected mode 0600, got %o", info.Mode().Perm())
	}
}

// The write allowlist is enforced: a path outside every write prefix is
// rejected even though the file does not yet exist.
func TestWriteConfigRejectsPathOutsideAllowlist(t *testing.T) {
	allowed := realTempDir(t)
	outside := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(outside, "evil.conf")
	if _, err := writeConfig(true, target, "x", "0644"); err == nil {
		t.Fatal("expected a path outside the write allowlist to be rejected")
	}
	if _, err := os.Stat(target); err == nil {
		t.Fatal("file must not have been written outside the allowlist")
	}
}

func TestWriteConfigRequiresPrivileged(t *testing.T) {
	allowed := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(allowed, "app.conf")
	if _, err := writeConfig(false, target, "x", "0644"); err == nil {
		t.Fatal("expected write_config to require privileged mode")
	}
}

func TestWriteConfigRejectsBadMode(t *testing.T) {
	allowed := realTempDir(t)
	t.Setenv("HP_AGENT_WRITE_PREFIXES", allowed)
	target := filepath.Join(allowed, "app.conf")
	if _, err := writeConfig(true, target, "x", "999"); err == nil {
		t.Fatal("expected an invalid octal mode to be rejected")
	}
	if _, err := writeConfig(true, target, "x", "abcd"); err == nil {
		t.Fatal("expected a non-numeric mode to be rejected")
	}
}
