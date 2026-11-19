package main

import (
	"context"
	"regexp"
	"sort"
	"strings"
)

// systemd units (#818): every service or timer that is active or enabled, with
// its state, its unit file and the PROGRAM its ExecStart runs - the path only.
// Never the arguments (they carry other services' secrets as --flags), never
// Environment, never the Description (a transient unit's description is its
// command line). systemctl's list/show verbs answer an unprivileged user.

const (
	maxUnits        = 400
	unitsPerShowRun = 40
)

type unitInfo struct {
	Name     string   `json:"name"`
	Type     string   `json:"type"`
	Active   string   `json:"active,omitempty"`
	Sub      string   `json:"sub,omitempty"`
	Enabled  string   `json:"enabled,omitempty"`
	Programs []string `json:"programs,omitempty"`
	UnitFile string   `json:"unit_file,omitempty"`
	// Custom: the unit file lives under /etc/systemd/system - put there by the
	// operator or a playbook, not by a package. These are the units a rebuild
	// has to bring back by hand.
	Custom bool `json:"custom,omitempty"`
}

type unitsSection struct {
	Status string     `json:"status"`
	Reason string     `json:"reason,omitempty"`
	Units  []unitInfo `json:"units"`
}

// execPath picks the program path out of one ExecStart group:
// "{ path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -g ... }".
var execPath = regexp.MustCompile(`\{ path=([^ ;]+)`)

func collectUnits(ctx context.Context, env describeEnv) unitsSection {
	listed, err := env.run(ctx, "systemctl", "list-units", "--type=service,timer", "--all", "--plain", "--no-legend", "--no-pager")
	if err != nil {
		status, reason := classifyErr(err)
		return unitsSection{Status: status, Reason: reason, Units: []unitInfo{}}
	}
	units := map[string]*unitInfo{}
	for _, line := range strings.Split(listed, "\n") {
		// UNIT LOAD ACTIVE SUB DESCRIPTION... - the description is dropped.
		f := strings.Fields(line)
		if len(f) < 4 || !isUnitName(f[0]) || f[1] == "not-found" {
			continue
		}
		units[f[0]] = &unitInfo{Name: f[0], Type: unitType(f[0]), Active: f[2], Sub: f[3]}
	}
	if files, err := env.run(ctx, "systemctl", "list-unit-files", "--type=service,timer", "--no-legend", "--no-pager"); err == nil {
		for _, line := range strings.Split(files, "\n") {
			f := strings.Fields(line)
			if len(f) < 2 || !isUnitName(f[0]) || strings.Contains(f[0], "@.") {
				continue
			}
			u, ok := units[f[0]]
			if !ok {
				u = &unitInfo{Name: f[0], Type: unitType(f[0])}
				units[f[0]] = u
			}
			u.Enabled = f[1]
		}
	}

	var kept []*unitInfo
	for _, u := range units {
		if u.Active == "active" || u.Enabled == "enabled" || u.Enabled == "enabled-runtime" {
			kept = append(kept, u)
		}
	}
	sort.Slice(kept, func(i, j int) bool { return kept[i].Name < kept[j].Name })
	if len(kept) > maxUnits {
		kept = kept[:maxUnits]
	}

	sec := unitsSection{Status: statusOK, Units: []unitInfo{}}
	for start := 0; start < len(kept); start += unitsPerShowRun {
		end := start + unitsPerShowRun
		if end > len(kept) {
			end = len(kept)
		}
		args := []string{"show", "--no-pager", "-p", "Id,ExecStart,FragmentPath,UnitFileState"}
		for _, u := range kept[start:end] {
			args = append(args, u.Name)
		}
		out, err := env.run(ctx, "systemctl", args...)
		if err != nil {
			sec.Status = statusPartial
			_, sec.Reason = classifyErr(err)
			continue
		}
		applyShow(out, units)
	}
	for _, u := range kept {
		sec.Units = append(sec.Units, *u)
	}
	return sec
}

// applyShow folds `systemctl show -p Id,ExecStart,FragmentPath,UnitFileState`
// blocks (separated by blank lines) into the unit records. Only the program
// path is taken from ExecStart.
func applyShow(out string, units map[string]*unitInfo) {
	for _, block := range strings.Split(out, "\n\n") {
		props := map[string]string{}
		for _, line := range strings.Split(block, "\n") {
			k, v, ok := strings.Cut(line, "=")
			if ok {
				props[k] = v
			}
		}
		u, ok := units[props["Id"]]
		if !ok {
			continue
		}
		for _, m := range execPath.FindAllStringSubmatch(props["ExecStart"], -1) {
			u.Programs = append(u.Programs, m[1])
		}
		if fp := props["FragmentPath"]; fp != "" {
			u.UnitFile = fp
			u.Custom = strings.HasPrefix(fp, "/etc/systemd/system/")
		}
		if u.Enabled == "" {
			u.Enabled = props["UnitFileState"]
		}
	}
}

var unitNameRe = regexp.MustCompile(`^[A-Za-z0-9_.@:\\-]+\.(service|timer)$`)

func isUnitName(s string) bool { return unitNameRe.MatchString(s) }

func unitType(name string) string {
	if strings.HasSuffix(name, ".timer") {
		return "timer"
	}
	return "service"
}
