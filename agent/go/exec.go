package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

type Executor struct{ allow Allowlist }

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
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
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
