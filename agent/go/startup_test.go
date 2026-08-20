package main

// #422: the SHIPPED BINARY must refuse to start when privileged mode is
// requested but cannot be honoured. This builds the real agent and runs it —
// nothing here is stubbed, because "the process exits non-zero with a usable
// diagnostic" is precisely the behaviour an operator depends on.

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func goTool(t *testing.T) string {
	t.Helper()
	candidate := filepath.Join(runtime.GOROOT(), "bin", "go")
	if _, err := os.Stat(candidate); err == nil {
		return candidate
	}
	if p, err := exec.LookPath("go"); err == nil {
		return p
	}
	t.Skip("no go toolchain available to build the agent binary")
	return ""
}

func buildAgent(t *testing.T) string {
	t.Helper()
	if testing.Short() {
		t.Skip("-short: skipping the agent binary build")
	}
	bin := filepath.Join(t.TempDir(), "hp-agent")
	cmd := exec.Command(goTool(t), "build", "-o", bin, ".")
	cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("building the agent failed: %v\n%s", err, out)
	}
	return bin
}

// runAgent runs the binary with the given env for at most d, returning its
// combined output and exit code (-1 when it was still running at the deadline).
func runAgent(t *testing.T, bin string, d time.Duration, env ...string) (string, int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), d)
	defer cancel()
	cmd := exec.CommandContext(ctx, bin)
	cmd.Env = append(os.Environ(), env...)
	out, err := cmd.CombinedOutput()
	code := 0
	if err != nil {
		code = -1
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			code = ee.ExitCode()
		}
	}
	if ctx.Err() == context.DeadlineExceeded {
		code = -1
	}
	return string(out), code
}

// The exact shipped misconfiguration: HP_AGENT_PRIVILEGED=true under a unit that
// runs the agent as a non-root user.
func TestBinaryRefusesPrivilegedModeAsNonRoot(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: cannot exercise the non-root refusal")
	}
	bin := buildAgent(t)
	out, code := runAgent(t, bin, 30*time.Second,
		"HP_AGENT_PRIVILEGED=true",
		"HP_AGENT_WRITE_PREFIXES="+t.TempDir(),
		"HP_AGENT_HUB_HOST=127.0.0.1",
		"HP_AGENT_HUB_PORT=1",
		"HP_AGENT_ID=test-agent",
		"HP_AGENT_ID_FILE="+filepath.Join(t.TempDir(), "agent.id"),
	)
	if code == 0 || code == -1 {
		t.Fatalf("expected a non-zero exit, got %d\n%s", code, out)
	}
	for _, want := range []string{"FATAL", "HP_AGENT_PRIVILEGED", "install-agent.sh --privileged"} {
		if !strings.Contains(out, want) {
			t.Errorf("diagnostic must contain %q, got:\n%s", want, out)
		}
	}
	if strings.Contains(out, "connecting to hub") {
		t.Error("the agent must refuse BEFORE connecting to the hub")
	}
}

// Control: the same binary, unprivileged, reports its self-check and keeps
// running (it retries the unreachable hub) instead of exiting. Without this the
// refusal above could be passing for the wrong reason.
func TestBinaryStartsUnprivilegedAndReportsItsSelfCheck(t *testing.T) {
	bin := buildAgent(t)
	out, code := runAgent(t, bin, 3*time.Second,
		"HP_AGENT_WRITE_PREFIXES="+t.TempDir(),
		"HP_AGENT_HUB_HOST=127.0.0.1",
		"HP_AGENT_HUB_PORT=1",
		"HP_AGENT_ID=test-agent",
		"HP_AGENT_ID_FILE="+filepath.Join(t.TempDir(), "agent.id"),
	)
	if code != -1 {
		t.Fatalf("an unprivileged agent must not exit (got exit %d):\n%s", code, out)
	}
	for _, want := range []string{"privileged mode: OFF", "write prefixes:", "writable"} {
		if !strings.Contains(out, want) {
			t.Errorf("startup report must contain %q, got:\n%s", want, out)
		}
	}
}
