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
