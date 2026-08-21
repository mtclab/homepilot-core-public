package main

// #422 gate: the systemd unit the installer generates must be able to do what
// the agent configuration it writes alongside it claims to grant.
//
// HONESTY NOTE — what this test is and is not.
//
// systemd is not PID 1 in the dev/CI environment (no `systemctl` to talk to, no
// mount namespaces to enter), so this is NOT a systemd integration test and does
// not pretend to be one. What it does instead is the closest honest thing:
//
//  1. it renders the unit + agent.env from the SHIPPED installer — the marked
//     grant/identity/unit blocks of scripts/install-agent.sh are sliced out and
//     executed verbatim by bash, so this asserts the real script, not a copy;
//  2. it parses the rendered unit;
//  3. it models systemd's write semantics (User=, ProtectSystem=,
//     ReadWritePaths=, PrivateTmp=, ProtectHome=) and asserts, for EVERY entry
//     in privilegedCommands and EVERY defaultWritePrefix, that the unit permits
//     what the allowlist permits — or that the agent itself refuses it.
//
// The model is documented at unitModel.canWrite. It encodes two systemd facts
// worth stating explicitly because they drive the design:
//   - under ProtectSystem=strict the whole hierarchy is read-only except the
//     ReadWritePaths entries (mode bits still look writable inside — only an
//     actual write reveals EROFS, which is why the agent's startup self-check
//     probes by writing);
//   - connecting to a unix socket is NOT blocked by a read-only mount (the
//     kernel only enforces EROFS for regular files, directories and symlinks),
//     which is why `systemctl` and the `docker` CLI still work under strict.
//
// The matrix is the part that would have caught the shipped bug: with
// HP_AGENT_PRIVILEGED=true, User=hp-agent, ProtectSystem=strict and
// ReadWritePaths=/etc/homepilot, 6 of the 7 write prefixes were read-only and
// every privileged command was impossible — while the suite stayed green.

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

const installerPath = "../../scripts/install-agent.sh"

// blockBetween slices the installer text between a "# >>> <name> block" marker
// line and its "# <<< end <name> block" line.
func blockBetween(t *testing.T, text, name string) string {
	t.Helper()
	start := strings.Index(text, "# >>> "+name+" block")
	end := strings.Index(text, "# <<< end "+name+" block")
	if start < 0 || end < 0 || end < start {
		t.Fatalf("could not locate the %q block markers in %s", name, installerPath)
	}
	nl := strings.Index(text[start:], "\n")
	return text[start+nl+1 : end]
}

type renderedInstall struct {
	unit unitModel
	env  map[string]string
	// prefixes is HP_AGENT_WRITE_PREFIXES split back out.
	prefixes []string
}

// renderInstall executes the installer's real grant + identity + unit blocks in
// a sandbox and returns the unit it wrote and the agent.env beside it.
func renderInstall(t *testing.T, args ...string) renderedInstall {
	t.Helper()
	raw, err := os.ReadFile(installerPath)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)

	privileged, allowPkg, extraPrefixes := "false", "false", ""
	for _, a := range args {
		switch {
		case a == "--privileged":
			privileged = "true"
		case a == "--allow-package-install":
			privileged, allowPkg = "true", "true"
		case strings.HasPrefix(a, "--write-prefix="):
			extraPrefixes += " " + strings.TrimPrefix(a, "--write-prefix=")
		default:
			t.Fatalf("unsupported arg for renderInstall: %s", a)
		}
	}

	dir := t.TempDir()
	confDir := filepath.Join(dir, "etc", "homepilot")
	unitPath := filepath.Join(dir, "hp-agent.service")
	script := strings.Join([]string{
		"set -euo pipefail",
		"SUDO=''",
		"PRIVILEGED=" + privileged,
		"ALLOW_PACKAGE_INSTALL=" + allowPkg,
		"WRITE_PREFIXES='" + extraPrefixes + "'",
		blockBetween(t, text, "grant"),
		"CONF_DIR='" + confDir + "'",
		"mkdir -p \"$CONF_DIR\"",
		"HUB_HOST=hub.example", "HUB_PORT=8443", "TOKEN=enrollment-token", "USE_TLS=false",
		"INSTALL_DIR=/usr/local/bin",
		"UNIT_PATH='" + unitPath + "'",
		blockBetween(t, text, "agent-identity"),
		blockBetween(t, text, "systemd-unit"),
	}, "\n")

	cmd := exec.Command("bash", "-c", script)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("installer blocks failed: %v\n%s", err, out)
	}

	unitText, err := os.ReadFile(unitPath)
	if err != nil {
		t.Fatal(err)
	}
	envText, err := os.ReadFile(filepath.Join(confDir, "agent.env"))
	if err != nil {
		t.Fatal(err)
	}
	env := map[string]string{}
	for _, line := range strings.Split(string(envText), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok {
			env[k] = v
		}
	}
	var prefixes []string
	for _, p := range strings.Split(env["HP_AGENT_WRITE_PREFIXES"], ":") {
		if p != "" {
			prefixes = append(prefixes, p)
		}
	}
	return renderedInstall{unit: parseUnit(string(unitText)), env: env, prefixes: prefixes}
}

// --- the systemd model ------------------------------------------------------

type unitModel struct {
	directives map[string]string
	User       string
	Group      string
	// ReadWritePaths with the "-" (tolerate-missing) marker stripped.
	ReadWritePaths []string
	ProtectSystem  string
	ProtectHome    string
	PrivateTmp     string
}

func parseUnit(text string) unitModel {
	u := unitModel{directives: map[string]string{}}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "[") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		u.directives[k] = v
	}
	u.User = u.directives["User"]
	u.Group = u.directives["Group"]
	u.ProtectSystem = u.directives["ProtectSystem"]
	u.ProtectHome = u.directives["ProtectHome"]
	u.PrivateTmp = u.directives["PrivateTmp"]
	for _, p := range strings.Fields(u.directives["ReadWritePaths"]) {
		u.ReadWritePaths = append(u.ReadWritePaths, strings.TrimPrefix(p, "-"))
	}
	return u
}

// hpOwnedDirs are the directories the installer creates and chowns to the
// service user, so a non-root agent can write them.
var hpOwnedDirs = []string{"/etc/homepilot", "/opt/homepilot", "/tmp/homepilot"}

// canWrite models whether the agent process, running under this unit, can write
// at path so that the write is visible on the host. Returns the reason when not.
func (u unitModel) canWrite(path string) (bool, string) {
	if u.PrivateTmp == "yes" && underAnyPrefix(path, []string{"/tmp"}) {
		return false, "PrivateTmp=yes: writes under /tmp are private to the unit and never reach the host"
	}
	if u.ProtectHome != "" && u.ProtectHome != "no" && underAnyPrefix(path, []string{"/home", "/root"}) {
		return false, "ProtectHome=" + u.ProtectHome
	}
	switch u.ProtectSystem {
	case "strict":
		if !underAnyPrefix(path, u.ReadWritePaths) {
			return false, fmt.Sprintf("ProtectSystem=strict and ReadWritePaths=%v does not cover it",
				u.ReadWritePaths)
		}
	case "full":
		if underAnyPrefix(path, []string{"/usr", "/boot", "/efi", "/etc"}) &&
			!underAnyPrefix(path, u.ReadWritePaths) {
			return false, "ProtectSystem=full"
		}
	case "yes":
		if underAnyPrefix(path, []string{"/usr", "/boot", "/efi"}) &&
			!underAnyPrefix(path, u.ReadWritePaths) {
			return false, "ProtectSystem=yes"
		}
	}
	if u.User != "root" && !underAnyPrefix(path, hpOwnedDirs) {
		return false, "User=" + u.User + " does not own " + path
	}
	return true, ""
}

// --- what each privileged allowlist entry needs from the unit ---------------

type commandNeed struct {
	// sample is a command string the allowlist accepts in the fully granted
	// configuration. The test verifies that, so a typo cannot make an entry pass
	// vacuously.
	sample string
	// needsRoot: the command cannot do its job as a non-root user.
	needsRoot bool
	// writes are specific paths the command must be able to write.
	writes []string
	// writesEveryPrefix: the command takes an unconstrained path, so the unit's
	// ReadWritePaths is what confines it — every granted prefix must be reachable.
	writesEveryPrefix bool
	// wholeFilesystem: the command writes wherever it likes (dpkg unpack), so the
	// unit must not impose a filesystem write boundary at all.
	wholeFilesystem bool
}

// privilegedCommandNeeds must have one entry per key of privilegedCommands. The
// completeness check below fails when a new privileged command is added without
// stating what the unit has to provide for it — that is the anti-drift half of
// this gate.
var privilegedCommandNeeds = map[string]commandNeed{
	// apt/dpkg unpack into /usr, /etc and /var: no write boundary can survive it.
	"apt-get": {sample: "apt-get install -y nginx", needsRoot: true, wholeFilesystem: true},
	"apt":     {sample: "apt install -y nginx", needsRoot: true, wholeFilesystem: true},
	// systemctl and the docker CLI only need root: they act by connecting to a
	// unix socket (PID 1 / dockerd), which a read-only mount does not block.
	// NOTE the double space in the docker-run sample: the shipped docker-run
	// regex requires it. That is a pre-existing allowlist quirk (it makes the
	// single-spaced form unusable), left alone here — #422 is about the runtime,
	// not about widening or reshaping the allowlist.
	"systemctl":         {sample: "systemctl restart nginx.service", needsRoot: true},
	"docker-run":        {sample: "docker run  nginx", needsRoot: true},
	"docker-pull":       {sample: "docker pull nginx:latest", needsRoot: true},
	"docker-compose-up": {sample: "docker compose -f /opt/homepilot/docker-compose.yml up -d", needsRoot: true},
	"docker-stop":       {sample: "docker stop web", needsRoot: true},
	// The file-manipulation entries take an unconstrained path in the regex, so
	// the unit's ReadWritePaths is what actually confines them: they must reach
	// every granted write prefix and nothing else.
	"mkdir": {sample: "mkdir -p /etc/nginx/conf.d", needsRoot: true, writesEveryPrefix: true},
	"chmod": {sample: "chmod 0644 /etc/nginx/nginx.conf", needsRoot: true, writesEveryPrefix: true},
	"cp":    {sample: "cp /opt/homepilot/a.conf /etc/nginx/a.conf", needsRoot: true, writesEveryPrefix: true},
	"mv":    {sample: "mv /opt/homepilot/a.conf /etc/nginx/a.conf", needsRoot: true, writesEveryPrefix: true},
	// A script artifact is delivered by writing it under /opt/homepilot first.
	"bash": {sample: "bash /opt/homepilot/apply.sh", needsRoot: true, writes: []string{"/opt/homepilot"}},
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// --- the gates --------------------------------------------------------------

// Completeness: every privileged allowlist entry states what the unit must give
// it, and every stated sample is genuinely accepted by the allowlist.
func TestPrivilegedCommandNeedsCoverTheAllowlist(t *testing.T) {
	for _, key := range sortedKeys(privilegedCommands) {
		need, ok := privilegedCommandNeeds[key]
		if !ok {
			t.Errorf("privilegedCommands[%q] has no entry in privilegedCommandNeeds: "+
				"state what the systemd unit must provide for it", key)
			continue
		}
		full := Allowlist{privileged: true, allowPackageInstall: true}
		if ok, reason := full.IsAllowed(need.sample); !ok {
			t.Errorf("sample for %q is not accepted by the allowlist (%s): %q",
				key, reason, need.sample)
		}
	}
	for _, key := range sortedKeys(privilegedCommandNeeds) {
		if _, ok := privilegedCommands[key]; !ok {
			t.Errorf("privilegedCommandNeeds[%q] describes a command that no longer exists", key)
		}
	}
}

// THE PREFIX MATRIX. For a privileged install, every write prefix the agent is
// configured with must be genuinely writable under the generated unit.
//
// Teeth: this is the assertion the shipped unit failed on 6 of 7 prefixes.
func TestPrivilegedUnitPermitsEveryConfiguredWritePrefix(t *testing.T) {
	r := renderInstall(t, "--privileged")
	if len(r.prefixes) == 0 {
		t.Fatal("installer wrote no HP_AGENT_WRITE_PREFIXES")
	}
	for _, p := range r.prefixes {
		if ok, why := r.unit.canWrite(p); !ok {
			t.Errorf("privileged install grants write prefix %s but the unit forbids it: %s", p, why)
		}
	}
	// …and the agent's built-in defaults are exactly what a default privileged
	// install grants, so fileops.go and the installer cannot drift apart.
	var want []string
	for _, p := range defaultWritePrefixes {
		want = append(want, strings.TrimRight(p, "/"))
	}
	sort.Strings(want)
	got := append([]string(nil), r.prefixes...)
	sort.Strings(got)
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("installer's default privileged prefixes %v != agent defaultWritePrefixes %v", got, want)
	}
}

// THE COMMAND MATRIX. For every privileged allowlist entry, the generated unit
// must be able to run it — or the agent must refuse it. "Granted but impossible"
// is the bug class this forbids.
func TestPrivilegedUnitPermitsOrAgentRefusesEveryPrivilegedCommand(t *testing.T) {
	cases := []struct {
		name string
		args []string
	}{
		{"privileged", []string{"--privileged"}},
		{"privileged+packages", []string{"--allow-package-install"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := renderInstall(t, tc.args...)
			allow := Allowlist{
				privileged:          r.env["HP_AGENT_PRIVILEGED"] == "true",
				allowPackageInstall: r.env["HP_AGENT_ALLOW_PACKAGE_INSTALL"] == "true",
			}
			for _, key := range sortedKeys(privilegedCommands) {
				need := privilegedCommandNeeds[key]
				permittedByAgent, _ := allow.IsAllowed(need.sample)
				runnable, why := unitCanRun(r, need)
				if permittedByAgent && !runnable {
					t.Errorf("%s: the agent accepts %q but the unit cannot run it: %s",
						key, need.sample, why)
				}
				if !permittedByAgent && runnable && need.wholeFilesystem {
					// Not a failure: the agent may narrow further than the unit.
					t.Logf("%s: unit would allow it, agent refuses it (narrower by design)", key)
				}
			}
		})
	}
}

// unitCanRun applies a command's stated needs to the rendered unit.
func unitCanRun(r renderedInstall, need commandNeed) (bool, string) {
	if need.needsRoot && r.unit.User != "root" {
		return false, "needs root, unit runs as User=" + r.unit.User
	}
	if need.wholeFilesystem {
		for _, p := range []string{"/usr/bin", "/var/lib/dpkg", "/etc/default"} {
			if ok, why := r.unit.canWrite(p); !ok {
				return false, "needs unrestricted filesystem writes: " + p + ": " + why
			}
		}
	}
	// Path-unconstrained file commands must reach every granted prefix.
	checks := append([]string(nil), need.writes...)
	if need.writesEveryPrefix {
		checks = append(checks, r.prefixes...)
	}
	for _, p := range checks {
		if ok, why := r.unit.canWrite(p); !ok {
			return false, p + ": " + why
		}
	}
	return true, ""
}

// The unit and agent.env are two halves of one decision and must agree exactly.
func TestUnitReadWritePathsMatchTheAgentConfiguration(t *testing.T) {
	for _, args := range [][]string{
		{},
		{"--privileged"},
		{"--allow-package-install"},
		{"--privileged", "--write-prefix=/srv/hp/"},
	} {
		r := renderInstall(t, args...)
		got := append([]string(nil), r.unit.ReadWritePaths...)
		want := append([]string(nil), r.prefixes...)
		sort.Strings(got)
		sort.Strings(want)
		if strings.Join(got, " ") != strings.Join(want, " ") {
			t.Errorf("%v: ReadWritePaths=%v != HP_AGENT_WRITE_PREFIXES=%v", args, got, want)
		}
	}
}

// The default install stays unprivileged, and is no weaker than before.
func TestDefaultInstallIsUnprivilegedAndHardened(t *testing.T) {
	r := renderInstall(t)
	if r.env["HP_AGENT_PRIVILEGED"] != "false" {
		t.Errorf("default install must be unprivileged, got %q", r.env["HP_AGENT_PRIVILEGED"])
	}
	if r.env["HP_AGENT_ALLOW_PACKAGE_INSTALL"] != "false" {
		t.Errorf("default install must not grant package management")
	}
	if r.unit.User != "hp-agent" {
		t.Errorf("default install must run as hp-agent, got %q", r.unit.User)
	}
	for k, want := range map[string]string{
		"NoNewPrivileges":       "yes",
		"ProtectSystem":         "strict",
		"ProtectHome":           "yes",
		"CapabilityBoundingSet": "",
		"PrivateDevices":        "yes",
		"RestrictSUIDSGID":      "yes",
		"SystemCallFilter":      "@system-service",
	} {
		got, ok := r.unit.directives[k]
		if !ok {
			t.Errorf("unprivileged unit is missing %s", k)
			continue
		}
		if got != want {
			t.Errorf("unprivileged unit %s=%q, want %q", k, got, want)
		}
	}
	// The system config dirs are NOT granted to a non-root agent: they could
	// never be written, so granting them would be the same lie in miniature.
	for _, p := range []string{"/etc/nginx", "/etc/systemd/system", "/etc/docker"} {
		for _, granted := range r.prefixes {
			if granted == p {
				t.Errorf("unprivileged install must not grant %s", p)
			}
		}
		if ok, _ := r.unit.canWrite(p); ok {
			t.Errorf("unprivileged unit must not be able to write %s", p)
		}
	}
}

// Package management is the one grant that removes the write boundary, and only
// when it is explicitly asked for.
func TestPackageGrantIsTheOnlyThingThatDropsTheWriteBoundary(t *testing.T) {
	priv := renderInstall(t, "--privileged")
	if priv.unit.ProtectSystem != "strict" {
		t.Errorf("a privileged install without package management must keep "+
			"ProtectSystem=strict, got %q", priv.unit.ProtectSystem)
	}
	pkg := renderInstall(t, "--allow-package-install")
	if pkg.unit.ProtectSystem != "no" {
		t.Errorf("granting package management must drop ProtectSystem (dpkg writes "+
			"everywhere), got %q", pkg.unit.ProtectSystem)
	}
	if pkg.env["HP_AGENT_PRIVILEGED"] != "true" {
		t.Error("--allow-package-install must imply --privileged")
	}
}

// The operator must be able to read, at install time, exactly what is granted.
func TestGrantSummaryNamesEveryPrivilegedCommand(t *testing.T) {
	raw, err := os.ReadFile(installerPath)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	start := strings.Index(text, "print_grant_summary() {")
	if start < 0 {
		t.Fatal("could not locate print_grant_summary in the installer")
	}
	end := strings.Index(text[start:], "\n}\n")
	if end < 0 {
		t.Fatal("could not find the end of print_grant_summary")
	}
	summary := text[start : start+end]
	for _, key := range sortedKeys(privilegedCommands) {
		if !strings.Contains(summary, key) {
			t.Errorf("the install-time grant summary never mentions %q — an operator "+
				"cannot see what they are granting", key)
		}
	}
}
