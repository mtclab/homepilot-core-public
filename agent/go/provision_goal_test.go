package main

// #422 goal-level gate: drive install_package / manage_service / write_config
// over the REAL wire protocol, through the REAL dispatch loop, the REAL command
// allowlist and the REAL os/exec + write path, and assert THE HOST STATE
// ACTUALLY CHANGED — not that the RPC returned status:ok. A suite that only
// asserts "the action returned success" is exactly what let a provisioning path
// that could never work stay green.
//
// HONESTY NOTE: systemd and dpkg are not available here (no PID 1, no package
// database), so `systemctl`, `dpkg` and `apt-get` are stub PROGRAMS placed on
// PATH that keep real state on disk. They are stubs of the system daemons, not
// mocks of the agent: every layer of the agent itself is the shipped one, and
// the assertions are about observable state (the unit's state file, the package
// db entry, the bytes and mode of the written file), not about return values.
// A true systemd integration test needs a VM/container with systemd as PID 1;
// that is the missing piece and this is deliberately the closest honest thing.

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const systemctlStub = `#!/bin/sh
# Stub systemctl: keeps unit state in $HP_TEST_STATE so the test can observe
# whether the service was ACTUALLY started/stopped.
verb="$1"; unit="$2"
state="$HP_TEST_STATE/unit.$unit"
echo "$*" >> "$HP_TEST_STATE/systemctl.log"
case "$verb" in
  is-active)
    if [ -f "$state" ]; then echo active; exit 0; else echo inactive; exit 3; fi ;;
  is-enabled)
    if [ -f "$state.enabled" ]; then echo enabled; exit 0; else echo disabled; exit 1; fi ;;
  start|restart) : > "$state"; exit 0 ;;
  stop) rm -f "$state"; exit 0 ;;
  enable) : > "$state.enabled"; exit 0 ;;
  disable) rm -f "$state.enabled"; exit 0 ;;
  *) echo "unsupported: $verb" >&2; exit 1 ;;
esac
`

const dpkgStub = `#!/bin/sh
# Stub dpkg: only "-s <name>" is used by the provisioner.
if [ "$1" = "-s" ]; then
  if [ -f "$HP_TEST_STATE/pkg.$2" ]; then
    echo "Status: install ok installed"
    exit 0
  fi
  echo "dpkg-query: package '$2' is not installed" >&2
  exit 1
fi
exit 1
`

const aptGetStub = `#!/bin/sh
# Stub apt-get: records the packages an "install -y" would have installed.
echo "$*" >> "$HP_TEST_STATE/apt.log"
[ "$1" = "install" ] || exit 1
shift
for a in "$@"; do
  [ "$a" = "-y" ] && continue
  : > "$HP_TEST_STATE/pkg.$a"
done
exit 0
`

// hostSandbox installs the stub system programs on PATH and returns the state
// directory they record into.
func hostSandbox(t *testing.T) string {
	t.Helper()
	bin := t.TempDir()
	state := t.TempDir()
	for name, body := range map[string]string{
		"systemctl": systemctlStub,
		"dpkg":      dpkgStub,
		"apt-get":   aptGetStub,
	} {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("HP_TEST_STATE", state)
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	return state
}

// liveAgent starts the shipped serve() loop over an in-memory connection and
// returns a round-trip function.
func liveAgent(t *testing.T, allow Allowlist) func(msg) msg {
	t.Helper()
	hub, agentSide := net.Pipe()
	a := &Agent{
		cfg:  Config{HeartbeatInterval: 3600},
		exec: Executor{allow: allow},
		conn: agentSide,
	}
	go func() { _ = a.serve() }()
	t.Cleanup(func() { _ = hub.Close(); _ = agentSide.Close() })

	return func(m msg) msg {
		t.Helper()
		b, err := encodeMessage(m)
		if err != nil {
			t.Fatal(err)
		}
		_ = hub.SetDeadline(time.Now().Add(30 * time.Second))
		if _, err := hub.Write(b); err != nil {
			t.Fatal(err)
		}
		resp, err := readMessage(hub)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
}

func changed(t *testing.T, resp msg) bool {
	t.Helper()
	if e := str(resp, "error"); e != "" {
		t.Fatalf("action failed: %s", e)
	}
	c, ok := resp["changed"].(bool)
	if !ok {
		t.Fatalf("response carries no changed flag: %v", resp)
	}
	return c
}

// manage_service must actually bring the unit to the desired state.
func TestManageServiceReachesTheDesiredState(t *testing.T) {
	state := hostSandbox(t)
	rt := liveAgent(t, Allowlist{privileged: true})
	unitState := filepath.Join(state, "unit.hp-demo.service")

	if _, err := os.Stat(unitState); err == nil {
		t.Fatal("precondition: the unit must start out inactive")
	}
	resp := rt(msg{"action": "manage_service", "name": "hp-demo.service",
		"state": "started", "request_id": "r1"})
	if !changed(t, resp) {
		t.Fatal("expected changed:true for a service that was not running")
	}
	// THE GOAL: the service is actually running now.
	if _, err := os.Stat(unitState); err != nil {
		t.Fatalf("manage_service reported success but the unit never started: %v", err)
	}

	// Idempotency, at the level of the goal: a second request leaves it running
	// and does not act again.
	resp = rt(msg{"action": "manage_service", "name": "hp-demo.service",
		"state": "started", "request_id": "r2"})
	if changed(t, resp) {
		t.Fatal("expected changed:false for an already-running service")
	}
	if _, err := os.Stat(unitState); err != nil {
		t.Fatal("the service must still be running")
	}

	// And it can actually be stopped again.
	resp = rt(msg{"action": "manage_service", "name": "hp-demo.service",
		"state": "stopped", "request_id": "r3"})
	if !changed(t, resp) {
		t.Fatal("expected changed:true when stopping a running service")
	}
	if _, err := os.Stat(unitState); err == nil {
		t.Fatal("manage_service reported success but the unit is still running")
	}
}

// install_package must actually leave the package installed.
func TestInstallPackageLeavesThePackageInstalled(t *testing.T) {
	state := hostSandbox(t)
	rt := liveAgent(t, Allowlist{privileged: true, allowPackageInstall: true})
	pkgFile := filepath.Join(state, "pkg.hp-demo-pkg")

	resp := rt(msg{"action": "install_package", "name": "hp-demo-pkg", "request_id": "r1"})
	if !changed(t, resp) {
		t.Fatal("expected changed:true for a package that was not installed")
	}
	// THE GOAL: the package is installed.
	if _, err := os.Stat(pkgFile); err != nil {
		t.Fatalf("install_package reported success but the package is not installed: %v", err)
	}
	resp = rt(msg{"action": "install_package", "name": "hp-demo-pkg", "request_id": "r2"})
	if changed(t, resp) {
		t.Fatal("expected changed:false for an already-installed package")
	}
}

// Without the package grant, the refusal must be explicit AND nothing may be
// installed — the failure mode this issue is about is "accepted, then nothing".
func TestInstallPackageWithoutTheGrantRefusesAndInstallsNothing(t *testing.T) {
	state := hostSandbox(t)
	rt := liveAgent(t, Allowlist{privileged: true})

	resp := rt(msg{"action": "install_package", "name": "hp-demo-pkg", "request_id": "r1"})
	errMsg := str(resp, "error")
	if errMsg == "" {
		t.Fatal("expected an error frame without the package grant")
	}
	if _, err := os.Stat(filepath.Join(state, "pkg.hp-demo-pkg")); err == nil {
		t.Fatal("nothing may be installed without the grant")
	}
	if _, err := os.Stat(filepath.Join(state, "apt.log")); err == nil {
		t.Fatal("apt-get must not even be invoked without the grant")
	}
}

// write_config must actually put the bytes on disk, at the requested mode, at a
// granted prefix.
func TestWriteConfigPutsTheBytesOnDisk(t *testing.T) {
	hostSandbox(t)
	prefix := t.TempDir()
	real, err := filepath.EvalSymlinks(prefix)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_WRITE_PREFIXES", real)
	rt := liveAgent(t, Allowlist{privileged: true})

	target := filepath.Join(real, "conf.d", "app.conf")
	body := "listen = 8080\n"
	resp := rt(msg{"action": "write_config", "path": target, "content": body,
		"mode": "0640", "request_id": "r1"})
	if !changed(t, resp) {
		t.Fatal("expected changed:true for a file that did not exist")
	}
	// THE GOAL: the file exists on disk with the right bytes and mode.
	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatalf("write_config reported success but wrote nothing: %v", err)
	}
	if string(got) != body {
		t.Fatalf("content = %q, want %q", got, body)
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o640 {
		t.Fatalf("mode = %o, want 0640", info.Mode().Perm())
	}
	resp = rt(msg{"action": "write_config", "path": target, "content": body,
		"mode": "0640", "request_id": "r2"})
	if changed(t, resp) {
		t.Fatal("expected changed:false for byte-identical content")
	}

	// A path outside every granted prefix must not be written.
	outside := filepath.Join(t.TempDir(), "escape.conf")
	resp = rt(msg{"action": "write_config", "path": outside, "content": "x",
		"mode": "0644", "request_id": "r3"})
	if str(resp, "error") == "" {
		t.Fatal("expected a path outside the write prefixes to be refused")
	}
	if _, err := os.Stat(outside); err == nil {
		t.Fatal("a refused write must not touch the filesystem")
	}
}

// The whole Phase-B journey on one host: write a config, then bring the service
// that consumes it up — the sequence host_provision.py actually drives.
func TestPhaseBJourneyConfigThenService(t *testing.T) {
	state := hostSandbox(t)
	prefix := t.TempDir()
	real, err := filepath.EvalSymlinks(prefix)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_WRITE_PREFIXES", real)
	rt := liveAgent(t, Allowlist{privileged: true, allowPackageInstall: true})

	steps := []msg{
		{"action": "install_package", "name": "hp-demo-pkg", "request_id": "j1"},
		{"action": "write_config", "path": filepath.Join(real, "hp-demo.conf"),
			"content": "mode = live\n", "mode": "0644", "request_id": "j2"},
		{"action": "manage_service", "name": "hp-demo.service", "state": "restarted",
			"request_id": "j3"},
	}
	for i, step := range steps {
		resp := rt(step)
		if e := str(resp, "error"); e != "" {
			t.Fatalf("step %d (%s) failed: %s", i+1, str(step, "action"), e)
		}
	}
	for _, want := range []string{
		filepath.Join(state, "pkg.hp-demo-pkg"),
		filepath.Join(real, "hp-demo.conf"),
		filepath.Join(state, "unit.hp-demo.service"),
	} {
		if _, err := os.Stat(want); err != nil {
			t.Errorf("journey left no trace at %s: %v", want, err)
		}
	}
}

// An unprivileged agent must refuse all three provisioning actions — and, again,
// change nothing.
func TestUnprivilegedAgentProvisionsNothing(t *testing.T) {
	state := hostSandbox(t)
	prefix := t.TempDir()
	real, err := filepath.EvalSymlinks(prefix)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HP_AGENT_WRITE_PREFIXES", real)
	rt := liveAgent(t, Allowlist{privileged: false})

	for i, m := range []msg{
		{"action": "install_package", "name": "hp-demo-pkg"},
		{"action": "manage_service", "name": "hp-demo.service", "state": "started"},
		{"action": "write_config", "path": filepath.Join(real, "x.conf"), "content": "x", "mode": "0644"},
	} {
		m["request_id"] = fmt.Sprintf("u%d", i)
		if e := str(rt(m), "error"); e == "" {
			t.Errorf("unprivileged agent must refuse %s", str(m, "action"))
		}
	}
	for _, unwanted := range []string{
		filepath.Join(state, "pkg.hp-demo-pkg"),
		filepath.Join(state, "unit.hp-demo.service"),
		filepath.Join(real, "x.conf"),
	} {
		if _, err := os.Stat(unwanted); err == nil {
			t.Errorf("unprivileged agent changed state at %s", unwanted)
		}
	}
}
