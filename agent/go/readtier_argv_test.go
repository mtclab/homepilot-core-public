package main

import (
	"encoding/json"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

// #817: the READ tier must never show a process's command line. A workspace
// manager passes session tokens as `--token=...` arguments, and `ps aux` (then
// part of the shared read policy) printed them to any read-scope caller.
//
// The journey: a real process carrying a sentinel in its argv is running; every
// command the read tier accepts - the shared corpus, every refused form the
// agent would still take, and every name-only ps column - is put through the
// agent's real allowlist and executed by the real Executor, and none of the
// output may contain the sentinel. A privileged `ps aux` DOES show it, so the
// sentinel is provably visible to ps and the test is not passing vacuously.
//
// Teeth: restore the pre-#817 ps pattern (`aux`, `-ef`, `-o args`) or
// `systemctl status` in readonly_policy.json and
// TestNoReadTierCommandShowsAnotherProcesssArguments fails on those forms.

const argvSentinel = "HPSENTINEL817c0ffee"

// TestHelperSentinelProcess is not a test: it is the process the journey looks
// for, re-executed from this test binary with the sentinel in its argv.
func TestHelperSentinelProcess(t *testing.T) {
	if os.Getenv("HP_TEST_SENTINEL_HELPER") != "1" {
		t.Skip("helper process only")
	}
	time.Sleep(2 * time.Minute)
	os.Exit(0)
}

func startSentinelProcess(t *testing.T) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestHelperSentinelProcess$", "--",
		"--token="+argvSentinel)
	cmd.Env = append(os.Environ(), "HP_TEST_SENTINEL_HELPER=1")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
	})
	// The process is visible in /proc once Start returns; give exec a moment to
	// replace the forked image so cmdline is the helper's.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		raw, _ := os.ReadFile("/proc/" + strconv.Itoa(cmd.Process.Pid) + "/cmdline")
		if strings.Contains(string(raw), argvSentinel) {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("the sentinel process never showed its argv in /proc")
}

// readTierCommands are the policy's command names the MCP read tier offers.
func readTierCommands(t *testing.T) map[string]bool {
	t.Helper()
	var p policyFile
	if err := json.Unmarshal(readonlyPolicyJSON, &p); err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	for name, c := range p.Commands {
		if c.ReadTier {
			out[name] = true
		}
	}
	return out
}

// psColumns are the column names the shared ps pattern admits, read back from
// the pattern itself so a column added there is exercised by the journey. nil
// when the pattern names no fixed column set (TestThePsColumnSetIsNameOnly
// fails on that; the journey still runs every other form).
func psColumns(t *testing.T) []string {
	t.Helper()
	var p policyFile
	if err := json.Unmarshal(readonlyPolicyJSON, &p); err != nil {
		t.Fatal(err)
	}
	pat := p.Commands["ps"].Pattern
	i := strings.Index(pat, `o\s+(`)
	if i < 0 {
		return nil
	}
	rest := pat[i+len(`o\s+(`):]
	return strings.Split(rest[:strings.Index(rest, ")")], "|")
}

// The read tier's ps columns are a fixed, name-only set.
func TestThePsColumnSetIsNameOnly(t *testing.T) {
	cols := psColumns(t)
	if len(cols) < 5 {
		t.Fatalf("the shared ps pattern does not name a fixed -o column set: %v", cols)
	}
	for _, col := range cols {
		switch col {
		case "args", "cmd", "command", "environ", "e", "cmdline":
			t.Errorf("the read tier's ps columns include %q, which prints argv or environment", col)
		}
	}
}

func TestNoReadTierCommandShowsAnotherProcesssArguments(t *testing.T) {
	defaultReadEnv(t)
	if _, err := exec.LookPath("ps"); err != nil {
		t.Skip("no ps on this host")
	}
	startSentinelProcess(t)

	// Control: the full listing an admin may run on a privileged agent shows
	// the argv - so ps can see the sentinel, and a clean read tier is a real
	// result rather than a blind ps.
	code, out, _ := Executor{allow: Allowlist{privileged: true}}.Exec("ps aux", 15)
	if code != 0 || !strings.Contains(out, argvSentinel) {
		t.Fatalf("control failed: privileged `ps aux` should show the sentinel (code %d)", code)
	}

	c := loadCorpus(t)
	candidates := []string{}
	for _, list := range [][]string{
		c.Commands.Allow, c.Commands.AgentOnly, c.Commands.RefuseReadTier, c.Commands.RefuseEverywhere,
	} {
		candidates = append(candidates, list...)
	}
	candidates = append(candidates,
		"systemctl show init.scope -p Id,LoadState,ActiveState,SubState,MainPID",
		"journalctl -n 200 --no-pager", "journalctl -n 200 -o short-full",
		"journalctl -n 200 -o cat", "journalctl -n 200 -o with-unit",
		"ss -tanp", "ss -xp",
	)
	if cols := psColumns(t); cols != nil {
		all := strings.Join(cols, ",")
		candidates = append(candidates,
			"ps -eo "+all, "ps -eHo "+all+" --forest", "ps -C hp-agent.test -o "+all)
		for _, col := range cols {
			candidates = append(candidates, "ps -eo pid,"+col)
		}
	}

	readTier := readTierCommands(t)
	unpriv := Executor{allow: Allowlist{privileged: false}}
	ran := 0
	for _, cmd := range candidates {
		base := firstField(cmd)
		if !readTier[base] {
			continue
		}
		if ok, _ := unpriv.allow.IsAllowed(cmd); !ok {
			continue
		}
		ran++
		code, stdout, stderr := unpriv.Exec(cmd, 15)
		if strings.Contains(stdout+stderr, argvSentinel) {
			t.Errorf("read-tier command %q printed another process's argv (exit %d)", cmd, code)
		}
	}
	if ran < 20 {
		t.Fatalf("only %d read-tier commands were exercised; the corpus parse is broken", ran)
	}
}

// A refusal names the safe form, and the form it names is accepted: a caller
// told no is told what to ask for instead, and that answer is not another no.
func TestARefusedArgvFormNamesTheSafeForm(t *testing.T) {
	defaultReadEnv(t)
	unpriv := Allowlist{privileged: false}
	for refused, safe := range map[string]string{
		"ps aux":                 "ps -eo pid,user,comm,etime,pcpu,pmem",
		"ps -ef":                 "ps -eo pid,user,comm,etime,pcpu,pmem",
		"ps -eo pid,args":        "ps -eo pid,user,comm,etime,pcpu,pmem",
		"systemctl status nginx": "systemctl show UNIT -p ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp",
		"journalctl -o json":     "-o short",
	} {
		ok, reason := unpriv.IsAllowed(refused)
		if ok {
			t.Errorf("unprivileged agent accepts %q", refused)
			continue
		}
		if !strings.Contains(reason, safe) {
			t.Errorf("refusal of %q does not name %q: %s", refused, safe, reason)
		}
	}
	for _, named := range []string{
		"ps -eo pid,user,comm,etime,pcpu,pmem",
		"systemctl show nginx -p ActiveState,SubState,MainPID,NRestarts,ActiveEnterTimestamp",
		"systemctl is-active nginx",
		"journalctl -u nginx",
		"journalctl -o short",
	} {
		if ok, reason := unpriv.IsAllowed(named); !ok {
			t.Errorf("the safe form a refusal names is itself refused: %q (%s)", named, reason)
		}
	}
}

// The full forms did not vanish: what the read tier refuses, a privileged agent
// still runs for an admin (the corpus's refuse_read_tier contract).
func TestAPrivilegedAgentKeepsTheFullForms(t *testing.T) {
	defaultReadEnv(t)
	c := loadCorpus(t)
	priv := Allowlist{privileged: true}
	for _, cmd := range c.Commands.RefuseReadTier {
		if ok, reason := priv.IsAllowed(cmd); !ok {
			t.Errorf("privileged agent refuses %q: %s", cmd, reason)
		}
	}
}
