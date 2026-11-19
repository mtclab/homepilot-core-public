package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #853: the read tier redacts unit files, so a rollback snapshot comes from the
// write path's own read (snapshot.go). It must return the exact bytes - or the
// revoke of nearly every unit an artifact overwrote could not put them back -
// and it must have the write path's authority and no more.
//
// Teeth: let snapshotFile redact (return redactUnitText's output) and
// TestASnapshotIsTheExactBytes fails; drop its privileged check, the write
// allowlist, the deny rules or O_NOFOLLOW and TestASnapshotHasTheWritePathsAuthority
// fails on that case.

func snapshotFixture(t *testing.T) (dir, unit, raw string) {
	t.Helper()
	defaultReadEnv(t)
	dir = t.TempDir()
	t.Setenv("HP_AGENT_WRITE_PREFIXES", dir)
	t.Setenv("HP_AGENT_CONFIG_DIR", filepath.Join(dir, "agent-conf"))
	raw = "[Service]\nEnvironment=TOKEN=" + unitSentinel("snapenv") + "\n" +
		"ExecStart=/usr/bin/app --token=" + unitSentinel("snaparg") + "\n"
	unit = filepath.Join(dir, "app.service.d", "override.conf")
	if err := os.MkdirAll(filepath.Dir(unit), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(unit, []byte(raw), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir, unit, raw
}

func TestASnapshotIsTheExactBytes(t *testing.T) {
	_, unit, raw := snapshotFixture(t)
	got, err := snapshotFile(true, unit)
	if err != nil || got != raw {
		t.Fatalf("snapshot of a unit is not its bytes (%v):\n%q\n%q", err, got, raw)
	}
	// ...while the read tier still never shows them.
	t.Setenv("HP_AGENT_READ_PREFIXES", filepath.Dir(unit))
	read, err := readFile(unit)
	if err != nil || strings.Contains(read, "hpSENTINEL853") {
		t.Fatalf("read_file of the same unit: %v\n%s", err, read)
	}
}

func TestASnapshotHasTheWritePathsAuthority(t *testing.T) {
	dir, unit, _ := snapshotFixture(t)
	outside := filepath.Join(t.TempDir(), "x.service")
	if err := os.WriteFile(outside, []byte("[Service]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	secret := filepath.Join(dir, "app.env")
	if err := os.WriteFile(secret, []byte("K=v\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "linked.service")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(dir, "agent-conf", "agent.token")
	if err := os.MkdirAll(filepath.Dir(state), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(state, []byte("tok\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]struct {
		privileged bool
		path, want string
	}{
		"unprivileged agent":       {false, unit, "requires privileged"},
		"outside the write prefix": {true, outside, "not in allowed write prefixes"},
		"a denied name":            {true, secret, "forbidden"},
		"through a symlink":        {true, link, "symbolic link"},
		"the agent's own state":    {true, state, "agent's own state"},
		"traversal":                {true, dir + "/../x", "traversal"},
		"absent":                   {true, filepath.Join(dir, "none.conf"), "file not found"},
	} {
		got, err := snapshotFile(c.privileged, c.path)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: want a refusal naming %q, got %q / %v", name, c.want, got, err)
		}
	}
}
