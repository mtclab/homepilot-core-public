package main

// Native, idempotent provisioning actions for managed-host onboarding (#397,
// phase B1). The spike chose native RPC actions over ansible because ansible
// needs a full target shell that would break the command allowlist.
//
// Containment: every action uses ONLY the agent's already-allowlisted primitives
// internally — the command Executor (which enforces the exact same allowlist as
// exec) for probes/actions, and the write-file allowlist + symlink safety for
// config writes. No arbitrary shell surface is added. Each action is idempotent
// (returns changed:false when the host is already in the desired state) and
// requires privileged mode (provisioning inherently runs privileged apt /
// systemctl). Every free-form argument is validated against a strict safe
// pattern BEFORE use — the same defense-in-depth reasoning the allowlist exists
// for.

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
)

// safeNameRe constrains package / service names. This is intentionally strict
// (a superset of neither the systemctl nor the apt allowlist argument classes,
// but a validation gate applied BEFORE the command is ever built) so an attacker
// cannot smuggle metacharacters or spaces through an argument.
var safeNameRe = regexp.MustCompile(`^[a-zA-Z0-9_.+-]+$`)

// safeModeRe constrains a file mode to 3-4 octal digits (e.g. "0644", "600").
var safeModeRe = regexp.MustCompile(`^[0-7]{3,4}$`)

// validServiceStates is the set of states manage_service accepts.
var validServiceStates = map[string]struct{}{
	"started":   {},
	"stopped":   {},
	"restarted": {},
	"enabled":   {},
	"disabled":  {},
}

// provisionResult is the structured outcome of a provisioning action.
type provisionResult struct {
	Changed bool
	Detail  string
}

// commandRunner is the injectable exec seam. In production this is
// Executor.Exec, which enforces the command allowlist; tests substitute a stub
// so the probe/action decisions can be exercised without touching the host.
type commandRunner func(command string, timeout int) (int, string, string)

const (
	probeTimeout   = 15  // read-only state probes (dpkg -s / systemctl is-*)
	serviceTimeout = 30  // systemctl start/stop/restart/enable/disable
	installTimeout = 300 // apt-get install can be slow
)

// installPackage installs an apt package idempotently. It first probes dpkg for
// the installed state (an allowlisted read-only `dpkg -s <name>`) and only runs
// the allowlisted `apt-get install -y <name>` when the package is not already
// installed. Requires privileged mode; validates name.
func installPackage(run commandRunner, privileged bool, name string) (provisionResult, error) {
	if !privileged {
		return provisionResult{}, fmt.Errorf("install_package requires privileged mode")
	}
	if !safeNameRe.MatchString(name) {
		return provisionResult{}, fmt.Errorf("invalid package name: %q", name)
	}
	// Idempotency probe: `dpkg -s <name>` reports "Status: install ok installed"
	// for a package that is actually installed. A removed-but-config package
	// reports a different status with exit 0, so we key on the status line, not
	// merely the exit code.
	code, stdout, _ := run(fmt.Sprintf("dpkg -s %s", name), probeTimeout)
	if code == 0 && strings.Contains(stdout, "install ok installed") {
		return provisionResult{Changed: false, Detail: name + " already installed"}, nil
	}
	code, _, stderr := run(fmt.Sprintf("apt-get install -y %s", name), installTimeout)
	if code != 0 {
		return provisionResult{}, fmt.Errorf("apt-get install %s failed (exit %d): %s",
			name, code, strings.TrimSpace(stderr))
	}
	return provisionResult{Changed: true, Detail: name + " installed"}, nil
}

// manageService brings a systemd unit to the desired state idempotently. It
// reads the current state first (allowlisted `systemctl is-active` /
// `is-enabled`) and applies the corresponding allowlisted action ONLY when a
// change is needed. `restarted` always acts (a restart has no idempotent
// no-op). Requires privileged mode; validates name + state.
func manageService(run commandRunner, privileged bool, name, state string) (provisionResult, error) {
	if !privileged {
		return provisionResult{}, fmt.Errorf("manage_service requires privileged mode")
	}
	if !safeNameRe.MatchString(name) {
		return provisionResult{}, fmt.Errorf("invalid service name: %q", name)
	}
	if _, ok := validServiceStates[state]; !ok {
		return provisionResult{}, fmt.Errorf("invalid service state: %q", state)
	}

	switch state {
	case "started", "stopped", "restarted":
		// is-active prints "active" when running; exit code is non-zero when not
		// active, so we read stdout rather than the exit code.
		_, out, _ := run(fmt.Sprintf("systemctl is-active %s", name), probeTimeout)
		active := strings.TrimSpace(out) == "active"
		switch state {
		case "started":
			if active {
				return provisionResult{Changed: false, Detail: name + " already active"}, nil
			}
			return applyService(run, "start", name)
		case "stopped":
			if !active {
				return provisionResult{Changed: false, Detail: name + " already stopped"}, nil
			}
			return applyService(run, "stop", name)
		default: // restarted
			return applyService(run, "restart", name)
		}
	default: // enabled / disabled
		_, out, _ := run(fmt.Sprintf("systemctl is-enabled %s", name), probeTimeout)
		enabled := strings.TrimSpace(out) == "enabled"
		if state == "enabled" {
			if enabled {
				return provisionResult{Changed: false, Detail: name + " already enabled"}, nil
			}
			return applyService(run, "enable", name)
		}
		if !enabled {
			return provisionResult{Changed: false, Detail: name + " already disabled"}, nil
		}
		return applyService(run, "disable", name)
	}
}

// applyService runs one allowlisted `systemctl <verb> <name>` and maps the
// outcome to a changed:true result (or an error on failure).
func applyService(run commandRunner, verb, name string) (provisionResult, error) {
	code, _, stderr := run(fmt.Sprintf("systemctl %s %s", verb, name), serviceTimeout)
	if code != 0 {
		return provisionResult{}, fmt.Errorf("systemctl %s %s failed (exit %d): %s",
			verb, name, code, strings.TrimSpace(stderr))
	}
	return provisionResult{Changed: true, Detail: fmt.Sprintf("%s %sed", name, verb)}, nil
}

// writeConfig writes a config file idempotently at an explicit mode. It enforces
// the SAME write allowlist + symlink safety as write_file (via resolveWritePath
// / writeFileMode) — arbitrary paths are rejected exactly as today. When the
// on-disk content is byte-identical the write is skipped (changed:false).
// Requires privileged mode; validates mode. The path is gated by the write
// allowlist rather than safeNameRe (it is a filesystem path, not a bare name).
func writeConfig(privileged bool, path, content, mode string) (provisionResult, error) {
	if !privileged {
		return provisionResult{}, fmt.Errorf("write_config requires privileged mode")
	}
	if !safeModeRe.MatchString(mode) {
		return provisionResult{}, fmt.Errorf("invalid mode: %q", mode)
	}
	parsed, err := strconv.ParseUint(mode, 8, 32)
	if err != nil {
		return provisionResult{}, fmt.Errorf("invalid mode: %q", mode)
	}
	// Enforce the write allowlist + symlink safety up front so a forbidden path
	// is rejected even when we would otherwise skip the write as unchanged.
	resolved, _, err := resolveWritePath(path)
	if err != nil {
		return provisionResult{}, err
	}
	// Idempotency: compare against the existing file's bytes (read from the
	// already allowlist-checked resolved path). A missing/unreadable file falls
	// through to a write.
	if existing, rerr := os.ReadFile(resolved); rerr == nil && string(existing) == content {
		return provisionResult{Changed: false, Detail: path + " already up to date"}, nil
	}
	if err := writeFileMode(path, content, os.FileMode(parsed)); err != nil {
		return provisionResult{}, err
	}
	return provisionResult{Changed: true, Detail: path + " written"}, nil
}
