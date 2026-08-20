package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// Mirrors hp_agent.file_ops, hardened per audit #378.
//
// Both read_file and write_file are now restricted:
//   - write_file may only land inside the write-allowed prefixes.
//   - read_file may only read inside the read-allowed prefixes (which include
//     the write prefixes) AND is subject to a denylist that always blocks
//     sensitive files even when they sit inside an allowed prefix.
//
// Paths are resolved through symlinks (EvalSymlinks) BEFORE the allowlist and
// denylist checks so a symlink cannot smuggle a target past either gate.

// defaultWritePrefixes is the built-in write allowlist. Overridable for tests
// (and constrained deployments) via HP_AGENT_WRITE_PREFIXES (colon-separated).
var defaultWritePrefixes = []string{
	"/etc/homepilot/", "/opt/homepilot/", "/tmp/homepilot/",
	"/etc/systemd/system/", "/etc/docker/", "/etc/nginx/",
}

// defaultReadPrefixes is the built-in read allowlist base. The effective read
// allowlist is this (or HP_AGENT_READ_PREFIXES) PLUS the write prefixes.
var defaultReadPrefixes = []string{
	"/etc", "/var/log", "/opt", "/srv", "/usr/local/etc", "/home",
}

var procEnvironRe = regexp.MustCompile(`^/proc/[^/]+/environ$`)

// privateKeyBaseRe matches base names that look like private keys.
var privateKeyBaseRe = regexp.MustCompile(`^(id_rsa|id_ed25519|id_ecdsa|id_dsa)$`)

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

func readAllowedPrefixes() []string {
	var base []string
	if v := os.Getenv("HP_AGENT_READ_PREFIXES"); v != "" {
		base = splitPrefixes(v)
	} else {
		base = append([]string(nil), defaultReadPrefixes...)
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
	switch resolved {
	case "/etc/shadow", "/etc/gshadow":
		return true, "access to sensitive file forbidden: " + resolved
	}
	base := filepath.Base(resolved)
	if privateKeyBaseRe.MatchString(base) ||
		strings.HasSuffix(base, ".key") || strings.HasSuffix(base, ".pem") {
		return true, "access to private key/credential forbidden: " + resolved
	}
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
	if procEnvironRe.MatchString(resolved) {
		return true, "access to process environ forbidden: " + resolved
	}
	return false, ""
}

func readFile(path string) (string, error) {
	resolved, err := resolvePath(path)
	if err != nil {
		return "", err
	}
	if denied, reason := isDenied(resolved); denied {
		return "", fmt.Errorf("%s", reason)
	}
	if !underAnyPrefix(resolved, readAllowedPrefixes()) {
		return "", fmt.Errorf("path not in allowed read prefixes: %s", path)
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
	return resolved, resolvedDir, nil
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
