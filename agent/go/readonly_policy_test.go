package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// #682 / #691: the agent's read-only exec set and its read_file paths come from
// ONE policy (readonly_policy.json, embedded), shared byte-for-byte with the
// control plane, and both layers run the same corpus
// (testdata/readonly_policy_corpus.json; the Python half is
// tests/test_readonly_policy.py).
//
// Found in the 2026-09-24 audit: the agent's safe set accepted `ip link set eth0
// down`, `ip route del default`, `ip addr flush`, `ss -K`, `systemctl
// daemon-reload` and `cat /etc/homepilot/agent.env` (validateCatPath never called
// isDenied), and read_file served /etc/ssh/ssh_host_*_key, /etc/pve/priv/*,
// /etc/wireguard/*, ceph keyrings, ~/.docker and ~/.kube credentials and
// /opt/homepilot/.env. On a privileged agent those ip/ss commands take the host
// off the network.
//
// Teeth: restore the old safeCommands regexes / catAllowedPrefixes / read
// prefixes and the corpus tests fail on exactly those verbs and paths.

type policyCorpus struct {
	Commands struct {
		Allow            []string `json:"allow"`
		AgentOnly        []string `json:"agent_only"`
		RefuseReadTier   []string `json:"refuse_read_tier"`
		RefuseEverywhere []string `json:"refuse_everywhere"`
	} `json:"commands"`
	ReadPaths struct {
		Allow  []string `json:"allow"`
		Refuse []string `json:"refuse"`
	} `json:"read_paths"`
}

func loadCorpus(t *testing.T) policyCorpus {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "readonly_policy_corpus.json"))
	if err != nil {
		t.Fatal(err)
	}
	var c policyCorpus
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	if len(c.Commands.Allow) == 0 || len(c.ReadPaths.Refuse) == 0 {
		t.Fatal("corpus parsed empty")
	}
	return c
}

// defaultReadEnv pins the agent to its shipped read configuration, whatever the
// developer's environment holds.
func defaultReadEnv(t *testing.T) {
	t.Setenv("HP_AGENT_READ_PREFIXES", "")
	t.Setenv("HP_AGENT_WRITE_PREFIXES", "")
	t.Setenv("HP_AGENT_CONFIG_DIR", "")
	t.Setenv("HP_AGENT_TOKEN_FILE", "")
}

func TestPolicyCorpusCommandsOnTheAgent(t *testing.T) {
	defaultReadEnv(t)
	c := loadCorpus(t)
	unpriv := Allowlist{privileged: false}
	for _, cmd := range append(append([]string{}, c.Commands.Allow...), c.Commands.AgentOnly...) {
		if ok, reason := unpriv.IsAllowed(cmd); !ok {
			t.Errorf("unprivileged agent refuses a read command %q: %s", cmd, reason)
		}
	}
	for _, cmd := range append(append([]string{}, c.Commands.RefuseReadTier...), c.Commands.RefuseEverywhere...) {
		if ok, _ := unpriv.IsAllowed(cmd); ok {
			t.Errorf("unprivileged agent accepts %q", cmd)
		}
	}
	// The network-mutating and secret-reading forms are refused on a PRIVILEGED
	// agent too: it runs them as root, which is what takes a host off the network.
	for _, a := range []Allowlist{{privileged: true}, {privileged: true, allowPackageInstall: true}} {
		for _, cmd := range c.Commands.RefuseEverywhere {
			if ok, _ := a.IsAllowed(cmd); ok {
				t.Errorf("privileged agent accepts %q", cmd)
			}
		}
	}
}

func TestPolicyCorpusReadPathsOnTheAgent(t *testing.T) {
	defaultReadEnv(t)
	c := loadCorpus(t)
	for _, p := range c.ReadPaths.Allow {
		if _, err := checkReadPath(p); err != nil {
			t.Errorf("agent refuses an allowlisted read %q: %v", p, err)
		}
	}
	for _, p := range c.ReadPaths.Refuse {
		if _, err := checkReadPath(p); err == nil {
			t.Errorf("agent would read %q", p)
		}
		// cat is the same check, not a side door around it.
		if ok, _ := (Allowlist{privileged: true}).IsAllowed("cat " + p); ok {
			t.Errorf("cat %q is accepted although read_file refuses it", p)
		}
	}
}

// Deny wins over an operator's widened prefixes: HP_AGENT_READ_PREFIXES may open
// a directory, never a credential inside it.
func TestDenyRulesSurviveWidenedReadPrefixes(t *testing.T) {
	defaultReadEnv(t)
	t.Setenv("HP_AGENT_READ_PREFIXES", "/etc:/home:/root:/opt")
	for _, p := range []string{
		"/etc/ssh/ssh_host_ed25519_key", "/etc/pve/priv/token.cfg", "/etc/wireguard/wg0.conf",
		"/home/user/.kube/config", "/home/user/.docker/config.json", "/root/.bashrc",
		"/opt/homepilot/.env", "/etc/shadow", "/etc/homepilot/agent.env",
	} {
		if _, err := checkReadPath(p); err == nil {
			t.Errorf("widened prefixes let %q through", p)
		}
	}
	if _, err := checkReadPath("/etc/fstab"); err != nil {
		t.Errorf("an operator-widened prefix should admit a non-secret file: %v", err)
	}
}

// A symlink cannot carry a denied target past the gate: the resolved path is
// what is checked, for read_file and cat alike.
func TestASymlinkToASecretIsRefused(t *testing.T) {
	defaultReadEnv(t)
	dir := t.TempDir()
	t.Setenv("HP_AGENT_READ_PREFIXES", dir)
	secretDir := filepath.Join(dir, ".ssh")
	if err := os.MkdirAll(secretDir, 0o700); err != nil {
		t.Fatal(err)
	}
	secret := filepath.Join(secretDir, "id_ed25519")
	if err := os.WriteFile(secret, []byte("PRIVATE"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "innocent.log")
	if err := os.Symlink(secret, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readFile(link); err == nil {
		t.Fatal("read_file followed a symlink to a private key")
	}
	code, out, _ := Executor{allow: Allowlist{}}.Exec("cat "+link, 5)
	if code == 0 || bytes.Contains([]byte(out), []byte("PRIVATE")) {
		t.Fatalf("cat followed a symlink to a private key: code=%d out=%q", code, out)
	}
}

// The agent's embedded policy is the control plane's policy, byte for byte.
func TestEmbeddedPolicyMatchesTheControlPlaneCopy(t *testing.T) {
	pyCopy, err := os.ReadFile(filepath.Join("..", "..", "src", "homepilot", "adapters", "readonly_policy.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(pyCopy, readonlyPolicyJSON) {
		t.Fatal("agent/go/readonly_policy.json and src/homepilot/adapters/readonly_policy.json differ; " +
			"edit the agent's copy and copy it verbatim")
	}
}
