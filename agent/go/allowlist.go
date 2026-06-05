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
		`^docker\s+run\s+(\s+--[a-zA-Z]+[= ][a-zA-Z0-9_.:/-]+)*\s+[a-zA-Z0-9_.:/-]+(\s+[a-zA-Z0-9_./-]+)*$`),
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
}

var catAllowedPrefixes = []string{
	"/var/log/", "/etc/homepilot/", "/etc/hostname", "/etc/os-release",
	"/etc/resolv.conf", "/etc/hosts", "/etc/systemd/", "/opt/",
}

var allowedSudoCommands = []string{
	"systemctl start", "systemctl stop", "systemctl restart",
	"systemctl enable", "systemctl disable",
	"apt-get install", "apt-get update", "apt install", "apt update",
	"docker pull", "docker run", "docker compose", "docker stop", "docker rm",
}

type Allowlist struct{ privileged bool }

func firstField(s string) string {
	f := strings.Fields(s)
	if len(f) == 0 {
		return ""
	}
	return f[0]
}

// IsAllowed mirrors CommandAllowlist.is_allowed → (allowed, reason).
func (a Allowlist) IsAllowed(command string) (bool, string) {
	stripped := strings.TrimSpace(command)
	if stripped == "" {
		return false, "empty command"
	}
	if shellMetaRe.MatchString(stripped) {
		return false, "shell metacharacters forbidden: " + command
	}

	usesSudo := strings.HasPrefix(stripped, "sudo ")
	actual := stripped
	if usesSudo {
		actual = strings.TrimSpace(stripped[5:])
	}

	if usesSudo {
		if !a.privileged {
			return false, "sudo requires privileged mode: " + command
		}
		for _, p := range allowedSudoCommands {
			if strings.HasPrefix(actual, p) {
				return true, ""
			}
		}
		return false, "sudo command not in allowlist: " + actual
	}

	base := firstField(actual)

	if a.privileged {
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
