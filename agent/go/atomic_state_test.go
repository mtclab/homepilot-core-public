package main

import (
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"testing"
	"time"
)

// The durable per-agent hub credential must be persisted ATOMICALLY.
//
// It was written with a plain os.WriteFile, which opens the destination with
// O_TRUNC: the old token is destroyed before the new one is on disk. A crash, a
// power cut or ENOSPC in that window leaves an empty or half-written token file.
// The agent then presents a credential the hub has no hash for, is rejected, and
// - the shared enrolment token having normally been removed from the host by
// then - is locked out for good, needing a hand re-enrol on every affected box.
//
// The two properties below are exactly what separates the atomic helper from
// os.WriteFile, and both are observable:
//
//   - the destination is only ever PUBLISHED by rename, so a successful write
//     replaces the inode instead of truncating the one that is already there;
//   - a write that cannot complete leaves the PREVIOUS token intact rather than
//     a truncated one.
//
// Teeth: change writeAgentTokenFile back to
// `os.WriteFile(path, []byte(token), 0o600)` and both fail - the first because
// the inode is unchanged, the second because the old token has become "".
func TestDurableTokenIsPersistedAtomically(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "agent.token")
	const old = "the-previous-durable-token"
	if err := os.WriteFile(path, []byte(old), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	before := inodeOf(t, path)

	const fresh = "a-freshly-minted-durable-token"
	if err := writeAgentTokenFile(path, fresh); err != nil {
		t.Fatalf("writeAgentTokenFile: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != fresh {
		t.Fatalf("token not persisted: got %q want %q", got, fresh)
	}
	if inodeOf(t, path) == before {
		t.Fatal("the token file was written in place (same inode): a truncating " +
			"write can leave a half-written credential and lock the agent out")
	}
	if mode := statMode(t, path); mode != 0o600 {
		t.Fatalf("token file mode = %o, want 600", mode)
	}
}

func TestAFailedTokenWriteKeepsTheUsableCredential(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: directory permissions do not constrain the write")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "agent.token")
	const old = "the-credential-that-still-authenticates"
	if err := os.WriteFile(path, []byte(old), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	// No new entries may be created in the directory. os.WriteFile would still
	// open the EXISTING file O_TRUNC and destroy the token; the atomic helper
	// cannot create its temp file and fails before touching anything.
	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatalf("chmod dir: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })

	if err := writeAgentTokenFile(path, "replacement-token"); err == nil {
		t.Fatal("expected the write to fail in an unwritable directory")
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != old {
		t.Fatalf("a failed write damaged the live credential: %q", got)
	}
}

// The agent adopts and persists a hub-minted durable token through the atomic
// path - the journey, not just the helper.
//
// Teeth: route register() back through os.WriteFile and the inode assertion
// fails, exactly as in the unit gate above.
func TestRegisterPersistsTheAdoptedTokenAtomically(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(tokenFile, []byte("shared-fleet-token"), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	before := inodeOf(t, tokenFile)

	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	a := &Agent{
		cfg:     Config{AuthToken: "shared-fleet-token", AuthTokenFile: tokenFile},
		agentID: "agent-atomic",
	}
	a.conn = client

	const minted = "minted-per-agent-token-0123456789"
	go func() {
		_ = server.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := readMessage(server); err != nil {
			return
		}
		ack, _ := encodeMessage(msg{
			"action":     "register_ack",
			"agent_id":   "agent-atomic",
			"auth_token": minted,
		})
		_, _ = server.Write(ack)
	}()

	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}
	b, err := os.ReadFile(tokenFile)
	if err != nil {
		t.Fatalf("token file not written: %v", err)
	}
	if string(b) != minted {
		t.Fatalf("persisted %q, want %q", b, minted)
	}
	if inodeOf(t, tokenFile) == before {
		t.Fatal("register() persisted the durable token in place; a crash mid-write " +
			"would leave a truncated credential")
	}
}

// The atomic helper must fsync the data before publishing the name. A rename is
// atomic in the directory entry, but on a crash the file's DATA can still be
// absent while the name already exists - which is the truncated-credential
// outcome this helper exists to prevent. fsync is not observable from inside the
// process, so this gate pins it in the source.
//
// Teeth: delete the f.Sync() call and this fails.
func TestAgentStateWritesAreFsyncedBeforeRename(t *testing.T) {
	src, err := os.ReadFile("config.go")
	if err != nil {
		t.Fatalf("read config.go: %v", err)
	}
	m := regexp.MustCompile(`(?s)func writeAgentStateFile\(.*?\n\}`).Find(src)
	if m == nil {
		t.Fatal("could not locate writeAgentStateFile in config.go")
	}
	body := string(m)
	sync := strings.Index(body, "f.Sync()")
	rename := strings.Index(body, "os.Rename(")
	if sync < 0 {
		t.Fatal("writeAgentStateFile no longer fsyncs: a rename can publish a name " +
			"whose data never reached the disk")
	}
	if rename < 0 || sync > rename {
		t.Fatal("the fsync must happen before the rename")
	}
}

func inodeOf(t *testing.T, path string) uint64 {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat %s: %v", path, err)
	}
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		t.Skip("no inode information on this platform")
	}
	return uint64(st.Ino)
}

func statMode(t *testing.T, path string) os.FileMode {
	t.Helper()
	fi, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat %s: %v", path, err)
	}
	return fi.Mode().Perm()
}
