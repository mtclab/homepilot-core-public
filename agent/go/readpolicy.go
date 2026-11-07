package main

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// The read-only policy shared with the control plane (#682, #691).
//
// readonly_policy.json is the ONE definition of what may be run read-only and
// what may be read. The control plane ships a byte-identical copy
// (src/homepilot/adapters/readonly_policy.json); a test on each side fails on
// drift, and both run testdata/readonly_policy_corpus.json. The hand-kept
// Go and Python tables it replaces had drifted apart, and both were shaped as
// "allow the command, deny a few bad arguments" - which is how `ip link set eth0
// down`, `ss -K`, `systemctl daemon-reload` and `cat /etc/homepilot/agent.env`
// all passed as read-only, and how host SSH keys and PVE secrets were readable.
//
// Commands are positive-only: the WHOLE command must match its pattern. Reads
// are default-deny: a path must be allowlisted and must not hit a deny rule, and
// deny always wins - over the allowlist, the write prefixes, and an operator's
// HP_AGENT_READ_PREFIXES alike.

//go:embed readonly_policy.json
var readonlyPolicyJSON []byte

type policyCommand struct {
	Pattern     string `json:"pattern"`
	ReadTier    bool   `json:"read_tier"`
	PathChecked bool   `json:"path_checked"`
}

type policyReadPaths struct {
	AllowPrefixes        []string `json:"allow_prefixes"`
	AllowExact           []string `json:"allow_exact"`
	DefaultWritePrefixes []string `json:"default_write_prefixes"`
	DenyExact            []string `json:"deny_exact"`
	DenyPrefixes         []string `json:"deny_prefixes"`
	DenyPathRegex        []string `json:"deny_path_regex"`
	DenyBasenameRegex    []string `json:"deny_basename_regex"`
}

type policyFile struct {
	ShellMetachars string                   `json:"shell_metachars"`
	Commands       map[string]policyCommand `json:"commands"`
	ReadPaths      policyReadPaths          `json:"read_paths"`
}

type compiledReadPaths struct {
	allowPrefixes []string
	allowExact    map[string]bool
	denyExact     map[string]bool
	denyPrefixes  []string
	denyPath      []*regexp.Regexp
	denyBasename  []*regexp.Regexp
}

var (
	// shellMetaRe rejects shell metacharacters (and CR/LF) before any pattern
	// is consulted. The agent never runs a shell; this keeps a command one
	// command.
	shellMetaRe *regexp.Regexp
	// safeCommands is the unprivileged command set, keyed by the first word.
	safeCommands map[string]*regexp.Regexp
	// pathCheckedCommands take one path argument that must pass checkReadPath.
	pathCheckedCommands map[string]bool
	// defaultWritePrefixes is the built-in write allowlist. Overridable for
	// tests (and constrained deployments) via HP_AGENT_WRITE_PREFIXES.
	defaultWritePrefixes []string
	readPolicy           compiledReadPaths
)

// wholeMatch anchors a shared pattern: the policy's patterns carry no anchors
// and must match the whole text (Python uses fullmatch for the same effect).
func wholeMatch(pattern string) *regexp.Regexp {
	return regexp.MustCompile(`^(?:` + pattern + `)$`)
}

func init() {
	var p policyFile
	if err := json.Unmarshal(readonlyPolicyJSON, &p); err != nil {
		panic(fmt.Sprintf("embedded readonly_policy.json is invalid: %v", err))
	}
	shellMetaRe = regexp.MustCompile("[" + regexp.QuoteMeta(p.ShellMetachars) + "]")
	safeCommands = make(map[string]*regexp.Regexp, len(p.Commands))
	pathCheckedCommands = map[string]bool{}
	for name, c := range p.Commands {
		safeCommands[name] = wholeMatch(c.Pattern)
		if c.PathChecked {
			pathCheckedCommands[name] = true
		}
	}
	rp := p.ReadPaths
	defaultWritePrefixes = append([]string(nil), rp.DefaultWritePrefixes...)
	readPolicy = compiledReadPaths{
		allowPrefixes: rp.AllowPrefixes,
		allowExact:    setOf(rp.AllowExact),
		denyExact:     setOf(rp.DenyExact),
		denyPrefixes:  rp.DenyPrefixes,
	}
	for _, rx := range rp.DenyPathRegex {
		readPolicy.denyPath = append(readPolicy.denyPath, wholeMatch(rx))
	}
	for _, rx := range rp.DenyBasenameRegex {
		readPolicy.denyBasename = append(readPolicy.denyBasename, wholeMatch(rx))
	}
}

func setOf(items []string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, s := range items {
		out[s] = true
	}
	return out
}

// policyDenies applies the policy's deny rules to one absolute path.
func policyDenies(p string) bool {
	if readPolicy.denyExact[p] || underAnyPrefix(p, readPolicy.denyPrefixes) {
		return true
	}
	for _, rx := range readPolicy.denyPath {
		if rx.MatchString(p) {
			return true
		}
	}
	base := filepath.Base(p)
	for _, rx := range readPolicy.denyBasename {
		if rx.MatchString(base) {
			return true
		}
	}
	return false
}

// readAllowed reports whether a (resolved) path is on the read allowlist:
// an allowlisted file, or under an allowlisted/operator read prefix or a write
// prefix (what HomePilot wrote it must be able to read back).
func readAllowed(p string) bool {
	return readPolicy.allowExact[p] || underAnyPrefix(p, readAllowedPrefixes())
}

// checkReadPath is THE read gate, shared by read_file and by `cat`: resolve the
// path through symlinks, refuse it if EITHER the path as given or its resolved
// target is denied, and require the resolved target to be allowlisted. It
// returns the resolved path, which is what the caller must then read.
func checkReadPath(path string) (string, error) {
	if !strings.HasPrefix(path, "/") {
		return "", fmt.Errorf("path must be absolute: %s", path)
	}
	resolved, err := resolvePath(path)
	if err != nil {
		return "", err
	}
	lexical := filepath.Clean(path)
	for _, p := range []string{lexical, resolved} {
		if denied, reason := isDenied(p); denied {
			return "", fmt.Errorf("%s", reason)
		}
	}
	if !readAllowed(resolved) {
		return "", fmt.Errorf("path not in the read allowlist: %s", path)
	}
	return resolved, nil
}
