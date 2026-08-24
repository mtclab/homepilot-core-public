package main

import (
	"regexp"
	"strings"
)

// CommandAllowlist ports hp_agent.command_executor.CommandAllowlist verbatim —
// same regexes, same decision order. Security-critical: keep in lockstep.

// shell metacharacters: | ; & ` $ < > ! # ( ) [ ]  (\x60 is backtick)
var shellMetaRe = regexp.MustCompile("[|;&\x60$<>!#()\\[\\]]")

var safeCommands = map[string]*regexp.Regexp{
	"cat":      regexp.MustCompile(`^cat\s+[^\s]+$`),
	"ls":       regexp.MustCompile(`^ls(\s+-[aAlrStZ]+)*(\s+[a-zA-Z0-9_./-]+)*$`),
	"ps":       regexp.MustCompile(`^ps(\s+[a-zA-Z0-9_-]+)*$`),
	"hostname": regexp.MustCompile(`^hostname$`),
	"uname":    regexp.MustCompile(`^uname(\s+-[aAmnrsv]+)*$`),
	"df":       regexp.MustCompile(`^df(\s+-[hHiTkP]+)*(\s+[a-zA-Z0-9_./-]+)?$`),
	"free":     regexp.MustCompile(`^free(\s+-[hkmgb]+)*$`),
	"uptime":   regexp.MustCompile(`^uptime$`),
	"ip":       regexp.MustCompile(`^ip\s+(addr|link|route|neigh)(\s+[a-zA-Z0-9_./-]+)*$`),
	"ss":       regexp.MustCompile(`^ss(\s+-[a-zA-Z0-9]+)*(\s+[a-zA-Z0-9_./-]+)?$`),
	"systemctl": regexp.MustCompile(
		`^systemctl\s+(status|is-active|is-enabled|daemon-reload)(\s+[a-zA-Z0-9_.-]+(\.service)?)?$`),
	"journalctl": regexp.MustCompile(`^journalctl(\s+-[a-zA-Z0-9]+(\s+\S+)?)*$`),
	"dpkg":       regexp.MustCompile(`^dpkg\s+-[lSs](\s+[a-zA-Z0-9_.+-]+)*$`),
	"docker": regexp.MustCompile(
		`^docker\s+(ps|images|inspect|logs|stats|version|info)(\s+[a-zA-Z0-9_.:/-]+)*$`),
}

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

var catAllowedPrefixes = []string{
	"/var/log/", "/etc/homepilot/", "/etc/hostname", "/etc/os-release",
	"/etc/resolv.conf", "/etc/hosts", "/etc/systemd/", "/opt/",
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
			if base == "cat" {
				return validateCatPath(actual)
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

func validateCatPath(actual string) (bool, string) {
	parts := strings.Fields(actual)
	if len(parts) < 2 {
		return false, "cat requires a path argument"
	}
	p := parts[1]
	if strings.Contains(p, "..") {
		return false, "path traversal forbidden: " + p
	}
	for _, prefix := range catAllowedPrefixes {
		if p == prefix || strings.HasPrefix(p, prefix) {
			return true, ""
		}
	}
	return false, "cat path not in allowed prefixes: " + p
}
