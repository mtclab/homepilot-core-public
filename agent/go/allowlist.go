package main

import (
	"regexp"
	"strings"
)

// The unprivileged (read-only) command set and the shell-metacharacter filter
// come from the policy shared with the control plane: see readpolicy.go and
// readonly_policy.json (#682). The privileged set below is agent-only - the
// control plane's READ tier never offers it.

var privilegedCommands = map[string]*regexp.Regexp{
	"apt-get": regexp.MustCompile(
		`^apt-get\s+(install|update|upgrade)\s+-y\s+[a-zA-Z0-9_.+-]+(\s+[a-zA-Z0-9_.+-]+)*$`),
	"apt": regexp.MustCompile(
		`^apt\s+(install|update|upgrade)\s+-y\s+[a-zA-Z0-9_.+-]+(\s+[a-zA-Z0-9_.+-]+)*$`),
	"docker-run": regexp.MustCompile(
		`^docker\s+run(\s+--[a-zA-Z]+[= ][a-zA-Z0-9_.:/-]+)*\s+[a-zA-Z0-9_.:/-]+(\s+[a-zA-Z0-9_./-]+)*$`),
	"docker-pull": regexp.MustCompile(`^docker\s+pull\s+[a-zA-Z0-9_.:/-]+$`),
	"docker-compose-up": regexp.MustCompile(
		`^docker\s+compose\s+(-f\s+[a-zA-Z0-9_./-]+\s+)?(up|down)\s+(-d\s+)?(--build\s+)?[a-zA-Z0-9_.-]*$`),
	"docker-stop": regexp.MustCompile(`^docker\s+(stop|rm|restart)\s+[a-zA-Z0-9_.:-]+$`),
	"systemctl": regexp.MustCompile(
		`^systemctl\s+(daemon-reload|start|stop|restart|enable|disable)(\s+[a-zA-Z0-9_.-]+(\.service)?)?$`),
	"mkdir": regexp.MustCompile(`^mkdir\s+-p\s+[a-zA-Z0-9_./-]+$`),
	"chmod": regexp.MustCompile(`^chmod\s+[0-7]{3,4}\s+[a-zA-Z0-9_./-]+$`),
	"cp":    regexp.MustCompile(`^cp\s+[a-zA-Z0-9_./-]+\s+[a-zA-Z0-9_./-]+$`),
	"mv":    regexp.MustCompile(`^mv\s+[a-zA-Z0-9_./-]+\s+[a-zA-Z0-9_./-]+$`),
	// Run a script HomePilot itself wrote under its own /opt/homepilot prefix.
	// Privileged-only and path-locked: a shell-script artifact is arbitrary code
	// by definition, so it requires privileged mode (like apt/systemctl) and may
	// only execute a .sh under the HP-controlled write prefix — never an
	// arbitrary path. The metachar filter still applies, so no chaining.
	// No '.' in the mid-path class so `..` traversal (e.g.
	// /opt/homepilot/../etc/x.sh) cannot match; only the final `.sh` has a dot.
	"bash": regexp.MustCompile(`^bash\s+/opt/homepilot/[a-zA-Z0-9_/-]+\.sh$`),
}

// dangerousDockerRunFlags are `docker run` options that hand the container the
// host outright: a bind mount of any path (including /), the host's PID/IPC/
// network namespaces, extra capabilities, a relaxed seccomp/apparmor profile, a
// raw device, a chosen uid, or a userns opt-out. Each is a full escape from the
// containment the allowlist exists to provide.
//
// This is a DENY pass, checked before the allow regex, and deliberately not
// expressed as regex surgery. The docker-run allow pattern matches a GENERIC
// "--flag value" shape, so it can never enumerate what is safe - widening it for
// one legitimate flag admits all of these at once, which is exactly what
// happened when #450 made the space-separated form reachable
// (`docker run --volume /:/mnt --user 0 img` was accepted).
//
// Every token after `run` is scanned, in both `--flag=value` and `--flag value`
// form and including the `-v` / `-u` short forms. Scanning the container's own
// argv too is intentional: telling the image name from its arguments requires
// knowing which docker flags take a value, and a parser that guesses wrong is a
// bypass. Refusing a container argument that happens to be spelled `-v` is a
// usability cost; letting `-v` through after a valueless flag is a root escape.
var dangerousDockerRunFlags = map[string]bool{
	"--volume":       true,
	"-v":             true,
	"--mount":        true,
	"--privileged":   true,
	"--user":         true,
	"-u":             true,
	"--pid":          true,
	"--ipc":          true,
	"--net":          true,
	"--network":      true,
	"--cap-add":      true,
	"--security-opt": true,
	"--device":       true,
	"--userns":       true,
}

// dockerRunDeniedFlag returns the first dangerous flag found in a `docker run`
// command's tokens, or "" when there is none. parts is the whitespace-split
// command including the leading `docker run`.
func dockerRunDeniedFlag(parts []string) string {
	for _, tok := range parts[2:] {
		name := tok
		if i := strings.Index(tok, "="); i >= 0 {
			name = tok[:i]
		}
		if dangerousDockerRunFlags[name] {
			return name
		}
	}
	return ""
}

// packageManagers are the privileged allowlist entries that install software.
// They are gated by a SEPARATE opt-in (HP_AGENT_ALLOW_PACKAGE_INSTALL) because
// unpacking a package writes across the whole filesystem (/usr, /etc, /var), so
// the systemd unit must drop its ProtectSystem= write boundary for them to work
// at all (#422). Gating them here means a host that was not granted package
// management is told so, instead of running apt and failing with EROFS.
var packageManagers = map[string]bool{"apt": true, "apt-get": true}

type Allowlist struct {
	privileged bool
	// allowPackageInstall additionally permits the apt/apt-get entries of
	// privilegedCommands. Meaningless without privileged.
	allowPackageInstall bool
}

func firstField(s string) string {
	f := strings.Fields(s)
	if len(f) == 0 {
		return ""
	}
	return f[0]
}

// IsAllowed mirrors CommandAllowlist.is_allowed → (allowed, reason).
//
// There is deliberately NO sudo path (#422). A privileged agent is installed as
// a root systemd unit, so it never needs to escalate; an unprivileged agent runs
// under NoNewPrivileges=yes with no sudoers entry, so sudo could never escalate
// even if it were permitted. Keeping a sudo parser would only re-open the
// wrapped-command bypass surface that #381 had to close. `sudo …` is therefore
// simply not an allowlisted command.
func (a Allowlist) IsAllowed(command string) (bool, string) {
	stripped := strings.TrimSpace(command)
	if stripped == "" {
		return false, "empty command"
	}
	if shellMetaRe.MatchString(stripped) {
		return false, "shell metacharacters forbidden: " + command
	}

	actual := stripped
	base := firstField(actual)

	if a.privileged {
		// Package management is a second, explicit grant: refuse it with a
		// diagnostic that names the fix rather than letting apt run and die on a
		// read-only filesystem.
		if packageManagers[base] && !a.allowPackageInstall {
			return false, "package management not granted on this host " +
				"(reinstall the agent with --allow-package-install): " + actual
		}
		parts := strings.Fields(actual)
		if base == "docker" && len(parts) >= 2 {
			sub := parts[1]
			subKey := ""
			switch {
			case sub == "pull" || sub == "run":
				subKey = "docker-" + sub
			case sub == "compose":
				subKey = "docker-compose-up"
			case sub == "stop" || sub == "rm" || sub == "restart":
				subKey = "docker-stop"
			}
			if subKey == "docker-run" {
				if flag := dockerRunDeniedFlag(parts); flag != "" {
					return false, "docker run flag not allowed (it escapes the " +
						"container): " + flag + " in " + actual
				}
			}
			if subKey != "" {
				if re, ok := privilegedCommands[subKey]; ok && re.MatchString(actual) {
					return true, ""
				}
			}
		}
		if re, ok := privilegedCommands[base]; ok && re.MatchString(actual) {
			return true, ""
		}
		if base == "docker" {
			if re, ok := safeCommands["docker"]; ok && re.MatchString(actual) {
				return true, ""
			}
		}
	}

	if re, ok := safeCommands[base]; ok {
		if re.MatchString(actual) {
			if pathCheckedCommands[base] {
				// `cat` reads through the SAME gate as read_file: resolve, deny,
				// allowlist. It used to check a prefix list and nothing else, so
				// it served the agent's own credentials (#682).
				if _, err := checkReadPath(strings.Fields(actual)[1]); err != nil {
					return false, base + ": " + err.Error()
				}
			}
			return true, ""
		}
		return false, "command pattern not allowed: " + actual
	}

	if a.privileged {
		return false, "privileged command not in allowlist: " + actual
	}
	return false, "command not in allowlist: " + base
}
