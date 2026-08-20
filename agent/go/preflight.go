package main

// Startup self-check for privileged mode (#422).
//
// Privileged mode is a REQUEST ("this agent may install packages, manage units
// and write system config"), not a capability. Whether the request can actually
// be honoured depends on the runtime the agent was installed into: the uid it
// runs as, and whether the systemd unit's ProtectSystem=/ReadWritePaths= mount
// namespace leaves the configured write prefixes writable.
//
// The shipped 2.x unit ran the agent as User=hp-agent under ProtectSystem=strict
// with ReadWritePaths=/etc/homepilot while the installer wrote
// HP_AGENT_PRIVILEGED=true. Every privileged action was therefore impossible,
// and nothing said so until the first hub request failed — hours later, as an
// opaque EROFS/EACCES on a remote host. This check turns that into a loud
// refusal to start, naming WHAT is wrong and HOW to fix it.
//
// Note on the probe: a read-only bind mount cannot be detected from the mode
// bits (they look perfectly writable inside the namespace). The only honest test
// is to attempt a write, so each prefix is probed by creating and immediately
// removing a temp file in it.

import (
	"fmt"
	"os"
	"strings"
)

type prefixState int

const (
	prefixWritable prefixState = iota
	prefixUnwritable
	prefixAbsent
)

type prefixStatus struct {
	Path  string
	State prefixState
	Err   error
}

func (s prefixStatus) String() string {
	switch s.State {
	case prefixWritable:
		return fmt.Sprintf("  %-24s writable", s.Path)
	case prefixAbsent:
		return fmt.Sprintf("  %-24s absent (nothing writes here until it exists)", s.Path)
	default:
		return fmt.Sprintf("  %-24s NOT WRITABLE: %v", s.Path, s.Err)
	}
}

// probeWritePrefix reports whether the agent can actually create a file under
// prefix right now. An absent directory is reported as such rather than created:
// creating e.g. /etc/nginx on a host that has no nginx would be a side effect,
// and under a correct strict unit the parent (/etc) is read-only anyway.
func probeWritePrefix(prefix string) prefixStatus {
	p := strings.TrimRight(prefix, "/")
	if p == "" {
		p = "/"
	}
	info, err := os.Stat(p)
	if err != nil {
		if os.IsNotExist(err) {
			return prefixStatus{Path: p, State: prefixAbsent}
		}
		return prefixStatus{Path: p, State: prefixUnwritable, Err: err}
	}
	if !info.IsDir() {
		return prefixStatus{Path: p, State: prefixUnwritable, Err: fmt.Errorf("not a directory")}
	}
	f, err := os.CreateTemp(p, ".hp-preflight-*")
	if err != nil {
		return prefixStatus{Path: p, State: prefixUnwritable, Err: err}
	}
	name := f.Name()
	_ = f.Close()
	_ = os.Remove(name)
	return prefixStatus{Path: p, State: prefixWritable}
}

func checkWritePrefixes(prefixes []string) []prefixStatus {
	out := make([]prefixStatus, 0, len(prefixes))
	for _, p := range prefixes {
		out = append(out, probeWritePrefix(p))
	}
	return out
}

// preflight builds the startup self-check report and, in privileged mode,
// returns a fatal error when the runtime cannot honour the privileged grant.
//
// euid is passed in (rather than read from the process) so the decision is
// testable without running the suite as root.
//
// Unprivileged mode never fails here: an unwritable prefix costs it a file
// write, not the whole privileged contract, and refusing to start would take a
// healthy monitoring agent offline over a directory it may never touch.
func preflight(cfg Config, euid int, prefixes []string) (report []string, err error) {
	if !cfg.Privileged {
		report = append(report, "privileged mode: OFF (safe allowlist only)")
		if cfg.AllowPackageInstall {
			report = append(report, "WARNING: HP_AGENT_ALLOW_PACKAGE_INSTALL has no effect "+
				"without HP_AGENT_PRIVILEGED; package management stays refused")
		}
		report = append(report, "write prefixes:")
		for _, s := range checkWritePrefixes(prefixes) {
			report = append(report, s.String())
			if s.State == prefixUnwritable {
				report = append(report,
					fmt.Sprintf("WARNING: %s is configured as a write prefix but is not writable; "+
						"write_file requests targeting it will fail", s.Path))
			}
		}
		return report, nil
	}

	grant := "NOT granted"
	if cfg.AllowPackageInstall {
		grant = "granted"
	}
	report = append(report, fmt.Sprintf("privileged mode: ON (package installs: %s)", grant))
	report = append(report, fmt.Sprintf("effective uid: %d", euid))

	if euid != 0 {
		return report, fmt.Errorf(
			"HP_AGENT_PRIVILEGED is set but the agent is running as euid %d, not root.\n"+
				"  Privileged actions (install_package / manage_service / write_config, and every\n"+
				"  apt-get / systemctl / docker command) need root and CANNOT work as this user.\n"+
				"  Fix: reinstall with `install-agent.sh --privileged` (it installs a root unit),\n"+
				"  or drop HP_AGENT_PRIVILEGED from /etc/homepilot/agent.env to run unprivileged",
			euid)
	}

	report = append(report, "write prefixes:")
	var bad []prefixStatus
	for _, s := range checkWritePrefixes(prefixes) {
		report = append(report, s.String())
		if s.State == prefixUnwritable {
			bad = append(bad, s)
		}
	}
	if len(bad) > 0 {
		var b strings.Builder
		fmt.Fprintf(&b, "HP_AGENT_PRIVILEGED is set but %d of the %d configured write prefixes "+
			"are not writable:\n", len(bad), len(prefixes))
		for _, s := range bad {
			fmt.Fprintf(&b, "    %s: %v\n", s.Path, s.Err)
		}
		b.WriteString(
			"  This is what a ProtectSystem=strict unit whose ReadWritePaths= does not list them\n" +
				"  looks like from inside: the mode bits say writable, the mount says no.\n" +
				"  Fix: reinstall with `install-agent.sh --privileged` so the unit's ReadWritePaths=\n" +
				"  matches HP_AGENT_WRITE_PREFIXES, or narrow HP_AGENT_WRITE_PREFIXES to the paths\n" +
				"  this host actually grants")
		return report, fmt.Errorf("%s", b.String())
	}
	return report, nil
}
