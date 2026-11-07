package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

type Executor struct{ allow Allowlist }

// execStreamBudget is the per-stream output cap. stdout and stderr travel in
// the SAME frame, so neither may claim the whole payload budget.
const execStreamBudget = maxPayloadBytes / 2

// capWriter accumulates at most `limit` bytes while counting everything written.
//
// An uncapped command buffer is two hazards at once. The frame the agent then
// builds exceeds the hub's 1 MiB limit, and the hub refuses to parse a body it
// cannot MAC-verify - so on a replay-protected connection (every agent holding
// a per-agent credential, i.e. the steady state) an oversize reply CLOSES the
// connection: `cat /var/log/syslog` knocked the host off the hub and returned
// the operator nothing. And buffering the whole stream means a multi-gigabyte
// log is read into memory by a process running as root.
//
// Counting past the limit is deliberate: the truncation notice states how much
// there actually was, which is the number an operator needs to decide what to
// do instead.
type capWriter struct {
	buf   []byte
	total int
	limit int
}

func (w *capWriter) Write(p []byte) (int, error) {
	w.total += len(p)
	if room := w.limit - len(w.buf); room > 0 {
		if len(p) < room {
			room = len(p)
		}
		w.buf = append(w.buf, p[:room]...)
	}
	return len(p), nil
}

// String is the captured output, with a notice appended when it was truncated.
func (w *capWriter) String() string {
	if w.total <= len(w.buf) {
		return string(w.buf)
	}
	return string(w.buf) + fmt.Sprintf(
		"\n[hp-agent] output truncated: %d bytes produced, %d returned "+
			"(the agent hub accepts at most %d bytes per reply)\n",
		w.total, len(w.buf), maxPayloadBytes)
}

const (
	// defaultExecTimeout applies when the hub sends no (or a non-positive)
	// timeout.
	defaultExecTimeout = 30 * time.Second
	// maxExecTimeout bounds what the hub may ask for (#720). The longest thing
	// the product runs is an apt install or a shell-script artifact at 300 s;
	// an unbounded value let one request hold a worker for as long as it liked.
	maxExecTimeout = 600 * time.Second
	// execWaitDelay is how long Wait keeps waiting for the output pipes after
	// the process group has been killed. A descendant that escaped the group
	// (setsid) could otherwise keep a pipe open and hold Wait for ever.
	execWaitDelay = 2 * time.Second
)

// clampExecTimeout turns the hub's timeout (seconds) into a bounded duration.
func clampExecTimeout(seconds int) time.Duration {
	if seconds <= 0 {
		return defaultExecTimeout
	}
	if seconds >= int(maxExecTimeout/time.Second) {
		return maxExecTimeout
	}
	return time.Duration(seconds) * time.Second
}

// Exec mirrors CommandExecutor.exec → (exitCode, stdout, stderr).
func (e Executor) Exec(command string, timeout int) (int, string, string) {
	allowed, reason := e.allow.IsAllowed(command)
	if !allowed {
		return -1, "", "command blocked: " + reason
	}
	fields := strings.Fields(command)
	if len(fields) == 0 {
		return -1, "", "empty command"
	}
	if pathCheckedCommands[fields[0]] {
		// `cat <path>` is answered by read_file itself rather than by spawning
		// cat on the path as given: the path was checked resolved, and handing
		// the unresolved one to a child would let a symlink swapped in between
		// the check and the read pick the target (#682). One gate, one read.
		content, err := readFile(fields[1])
		if err != nil {
			return 1, "", fields[0] + ": " + err.Error()
		}
		return 0, content, ""
	}
	limit := clampExecTimeout(timeout)
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()

	cmd := exec.CommandContext(ctx, fields[0], fields[1:]...)
	// The command runs in its OWN process group, and a timeout kills the whole
	// group (#720). Killing only the direct child is not enough: a shell that
	// started `sleep` leaves the sleep holding stdout, and Wait blocks until
	// every writer of the pipe is gone - a 1 s timeout came back after 8 s.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		// Negative pid = the process group led by the child.
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	cmd.WaitDelay = execWaitDelay
	stdout := &capWriter{limit: execStreamBudget}
	stderr := &capWriter{limit: execStreamBudget}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err := cmd.Run()

	if ctx.Err() == context.DeadlineExceeded {
		return -1, "", fmt.Sprintf("command timed out after %ds", int(limit/time.Second))
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return exitErr.ExitCode(), stdout.String(), stderr.String()
		}
		if errors.Is(err, exec.ErrNotFound) {
			return -1, "", "command not found: " + fields[0]
		}
		return -1, "", err.Error()
	}
	return 0, stdout.String(), stderr.String()
}
