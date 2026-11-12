package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Mirrors hp_agent.file_ops, hardened per audit #378 and #691.
//
// Both read_file and write_file are restricted:
//   - write_file may only land inside the write-allowed prefixes.
//   - read_file is DEFAULT-DENY under the shared read policy (readpolicy.go,
//     readonly_policy.json): a path must be an allowlisted file or sit under an
//     allowlisted prefix (logs, unit files) or a write prefix, and it is refused
//     by the policy's deny rules and the agent's own credential checks below
//     even when an allowlist entry - or an operator's HP_AGENT_READ_PREFIXES -
//     covers it. The old default opened all of /etc, /opt, /srv and /home, which
//     served host SSH keys, PVE secrets and service credentials at the READ tier.
//
// Paths are resolved through symlinks (EvalSymlinks) BEFORE the allowlist and
// denylist checks so a symlink cannot smuggle a target past either gate.
// defaultWritePrefixes comes from the shared policy (readpolicy.go).

var procEnvironRe = regexp.MustCompile(`^/proc/[^/]+/environ$`)

// agentSecretBases are the agent's OWN credential files. The agent must never
// hand back the material that lets something else become an agent.
//
// `agent.token` (the durable per-agent credential) was already refused, but
// only when HP_AGENT_TOKEN_FILE named it - and `agent.env` was not refused at
// all. That file is what scripts/install-agent.sh writes, and it carries
// HP_AGENT_AUTH_TOKEN: the SHARED FLEET ENROLMENT TOKEN, plus the hub's address
// and certificate pin. The hub refuses to serve that token over MCP at all
// (GET /agents/token and the installer one-liner are both excluded by name:
// "a credential that provisions machines must not appear in an MCP transcript")
// - and then read_file_on_guest, a READ-tier tool, read it straight off the
// host instead. Found live on dev, review #648.
//
// Matched by base name under the agent's configuration directory rather than by
// denying that directory outright: /etc/homepilot is also a granted WRITE
// prefix, so artifacts legitimately put files there and must stay readable.
var agentSecretBases = map[string]bool{
	"agent.env":   true,
	"agent.token": true,
	// The one previous copy an installer keeps when it re-enrols (#815).
	"agent.token.prev": true,
}

// agentConfigDir is where the installer keeps the agent's identity and
// credentials. Overridable for tests and for a constrained deployment that
// moved them, the same way the prefixes are.
func agentConfigDir() string {
	if v := strings.TrimSpace(os.Getenv("HP_AGENT_CONFIG_DIR")); v != "" {
		return strings.TrimRight(v, "/")
	}
	return "/etc/homepilot"
}

func splitPrefixes(v string) []string {
	parts := strings.Split(v, ":")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func writeAllowedPrefixes() []string {
	if v := os.Getenv("HP_AGENT_WRITE_PREFIXES"); v != "" {
		return splitPrefixes(v)
	}
	return defaultWritePrefixes
}

// readAllowedPrefixes is the policy's read prefixes (or an operator's
// HP_AGENT_READ_PREFIXES in their place) plus the write prefixes. The policy's
// deny rules still apply to everything under them.
func readAllowedPrefixes() []string {
	var base []string
	if v := os.Getenv("HP_AGENT_READ_PREFIXES"); v != "" {
		base = splitPrefixes(v)
	} else {
		base = append([]string(nil), readPolicy.allowPrefixes...)
	}
	return append(base, writeAllowedPrefixes()...)
}

// underAnyPrefix reports whether the (already resolved) absolute path p falls
// under any of the given prefixes. A trailing slash on a prefix is ignored;
// matching is boundary-aware so "/etc" does not match "/etcfoo".
func underAnyPrefix(p string, prefixes []string) bool {
	for _, pre := range prefixes {
		pre = strings.TrimRight(pre, "/")
		if pre == "" {
			continue
		}
		if p == pre || strings.HasPrefix(p, pre+"/") {
			return true
		}
	}
	return false
}

// resolvePath rejects lexical traversal, makes the path absolute, then resolves
// symlinks. If EvalSymlinks fails (e.g. a not-yet-existing parent) it falls back
// to the lexical absolute path.
func resolvePath(path string) (string, error) {
	if strings.Contains(path, "..") {
		return "", fmt.Errorf("path traversal forbidden: %s", path)
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved, nil
	}
	return abs, nil
}

// isDenied returns true (with a reason) for paths that must never be read even
// when they sit inside an allowed prefix. The argument must already be resolved.
func isDenied(resolved string) (bool, string) {
	if policyDenies(resolved) {
		return true, "access to a credential or private key forbidden: " + resolved
	}
	base := filepath.Base(resolved)
	if tf := os.Getenv("HP_AGENT_TOKEN_FILE"); tf != "" {
		tfResolved := tf
		if r, err := filepath.EvalSymlinks(tf); err == nil {
			tfResolved = r
		} else if a, err := filepath.Abs(tf); err == nil {
			tfResolved = a
		}
		if tfResolved == resolved {
			return true, "access to agent token file forbidden"
		}
	}
	// The agent's own credential files, wherever HP_AGENT_TOKEN_FILE points (or
	// does not point at all - a bootstrap-enrolled agent has no token file yet,
	// and agent.env holds the shared token regardless).
	if filepath.Dir(resolved) == agentConfigDir() && agentSecretBases[base] {
		return true, "access to the agent's own credentials forbidden: " + resolved
	}
	if procEnvironRe.MatchString(resolved) {
		return true, "access to process environ forbidden: " + resolved
	}
	return false, ""
}

func readFile(path string) (string, error) {
	resolved, err := checkReadPath(path)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fmt.Errorf("file not found: %s", path)
		}
		return "", err
	}
	if info.IsDir() {
		return "", fmt.Errorf("not a file: %s", path)
	}
	// Refuse BEFORE reading, on the stat: a reply this size cannot cross the hub
	// protocol, and on a replay-protected connection the hub answers an oversize
	// frame by closing the socket - so reading it would cost the host its agent
	// connection AND still return nothing. Refusing here also keeps the agent
	// from allocating a multi-gigabyte log file into memory as root.
	if info.Size() > maxPayloadBytes {
		return "", fmt.Errorf(
			"file is %d bytes; the agent hub accepts at most %d per reply. "+
				"Filter it on the host (tail/grep) or copy it out of band: %s",
			info.Size(), maxPayloadBytes, path)
	}
	data, err := os.ReadFile(resolved)
	if err != nil {
		if os.IsPermission(err) {
			return "", fmt.Errorf("permission denied: %s", path)
		}
		return "", err
	}
	return string(data), nil
}

func writeFile(path, content string) error {
	return writeFileMode(path, content, 0o644)
}

// resolveWritePath rejects lexical traversal, makes the path absolute, resolves
// the destination DIRECTORY through symlinks (so a symlinked parent cannot
// smuggle the target elsewhere), rejoins the base name, and enforces the write
// allowlist on the resolved result. It returns the resolved destination path and
// the resolved parent directory. This is the single write-side gate reused by
// both writeFileMode and the provisioning write_config action — keep it in one
// place so neither path can weaken it.
func resolveWritePath(path string) (resolved, resolvedDir string, err error) {
	if strings.Contains(path, "..") {
		return "", "", fmt.Errorf("path traversal forbidden: %s", path)
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", "", err
	}
	dir := filepath.Dir(abs)
	resolvedDir = dir
	if r, err := filepath.EvalSymlinks(dir); err == nil {
		resolvedDir = r
	}
	resolved = filepath.Join(resolvedDir, filepath.Base(abs))
	if !underAnyPrefix(resolved, writeAllowedPrefixes()) {
		return "", "", fmt.Errorf("path not in allowed write prefixes (%s): %s",
			strings.Join(writeAllowedPrefixes(), ", "), path)
	}
	if isAgentStateFile(resolved) {
		return "", "", fmt.Errorf("refusing to overwrite the agent's own state file "+
			"(identity, credential, transport or environment): %s", path)
	}
	return resolved, resolvedDir, nil
}

// agentStateNames are the files the agent keeps in its configuration directory:
// its identity, its credential, the hub-pushed transport and the installer's
// environment file.
var agentStateNames = []string{"agent.id", "agent.token", "agent.token.prev", "agent.transport", "agent.env"}

// agentStateFiles lists where THIS agent keeps its state: the default names in
// its config directory and beside its token file, plus wherever the env moved
// the id, transport or environment file. Every entry has its directory resolved
// through symlinks, so it compares equal to a resolved write target.
func agentStateFiles() []string {
	tokenFile := os.Getenv("HP_AGENT_TOKEN_FILE")
	paths := []string{
		agentIDFilePath(os.Getenv("HP_AGENT_ID_FILE"), tokenFile),
		transportFilePath(os.Getenv("HP_AGENT_TRANSPORT_FILE"), tokenFile),
	}
	if tokenFile != "" {
		paths = append(paths, tokenFile)
	}
	if envFile := os.Getenv("HP_AGENT_ENV_FILE"); envFile != "" {
		paths = append(paths, envFile)
	}
	dirs := []string{agentConfigDir()}
	if tokenFile != "" {
		dirs = append(dirs, filepath.Dir(tokenFile))
	}
	for _, dir := range dirs {
		for _, name := range agentStateNames {
			paths = append(paths, filepath.Join(dir, name))
		}
	}
	out := make([]string, 0, len(paths))
	for _, p := range paths {
		abs, err := filepath.Abs(p)
		if err != nil {
			continue
		}
		dir := filepath.Dir(abs)
		if r, err := filepath.EvalSymlinks(dir); err == nil {
			dir = r
		}
		out = append(out, filepath.Join(dir, filepath.Base(abs)))
	}
	return out
}

// isAgentStateFile reports whether a resolved write target is one of the
// agent's own state files (#723).
//
// /etc/homepilot/ is a write prefix on every grant, because artifacts put their
// own files there - and so, with no check, the hub's write_file could replace
// the agent's transport (an unparseable pin there fails every later connect,
// with no channel left to fix it), its credential or id (orphaning it), or
// agent.env (widening HP_AGENT_READ_PREFIXES for the next start). The agent
// writes these itself, through writeAgentStateFile; nothing else may.
func isAgentStateFile(resolved string) bool {
	for _, p := range agentStateFiles() {
		if p == resolved {
			return true
		}
	}
	return false
}

// writeFileMode resolves the destination directory through symlinks, re-checks
// the write allowlist on the resolved path (symlink defense), then writes
// atomically: temp file in the destination dir -> chmod -> Sync -> Rename.
func writeFileMode(path, content string, mode os.FileMode) error {
	resolved, resolvedDir, err := resolveWritePath(path)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(resolvedDir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(resolvedDir, ".hp-write-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	// Best-effort cleanup; the Remove is a no-op once Rename has moved the file.
	defer os.Remove(tmpName)
	if _, err := tmp.WriteString(content); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, resolved)
}
