package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
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
	if timeout <= 0 {
		timeout = 30
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeout)*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, fields[0], fields[1:]...)
	stdout := &capWriter{limit: execStreamBudget}
	stderr := &capWriter{limit: execStreamBudget}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err := cmd.Run()

	if ctx.Err() == context.DeadlineExceeded {
		return -1, "", fmt.Sprintf("command timed out after %ds", timeout)
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
