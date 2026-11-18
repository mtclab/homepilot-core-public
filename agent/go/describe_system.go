package main

import (
	"context"
	"encoding/json"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Firewall, pending updates and time sync (#818).
//
// The firewall is a SUMMARY: ufw's enabled flag (ufw.conf is world-readable)
// and its rules (user.rules, root-only on a stock host), nftables table/chain
// names with rule counts (`nft` needs CAP_NET_ADMIN). What the agent's user
// cannot read is reported as not visible, never guessed from a partial view.
// Pending updates come from update-notifier's file, then from an apt
// simulation; time sync from timedatectl.

const maxFirewallRules = 200

type ufwRule struct {
	Action    string `json:"action"`
	Proto     string `json:"proto"`
	Port      string `json:"port"`
	From      string `json:"from"`
	Direction string `json:"direction"`
}

type ufwSection struct {
	Status      string    `json:"status"`
	Reason      string    `json:"reason,omitempty"`
	Enabled     *bool     `json:"enabled,omitempty"`
	RulesStatus string    `json:"rules_status,omitempty"`
	Rules       []ufwRule `json:"rules"`
}

type nftChain struct {
	Name   string `json:"name"`
	Hook   string `json:"hook,omitempty"`
	Policy string `json:"policy,omitempty"`
	Rules  int    `json:"rules"`
}

type nftTable struct {
	Family string     `json:"family"`
	Name   string     `json:"name"`
	Chains []nftChain `json:"chains"`
}

type nftSection struct {
	Status string     `json:"status"`
	Reason string     `json:"reason,omitempty"`
	Tables []nftTable `json:"tables"`
}

type firewallSection struct {
	UFW      ufwSection `json:"ufw"`
	Nftables nftSection `json:"nftables"`
}

func collectFirewall(ctx context.Context, env describeEnv) firewallSection {
	return firewallSection{UFW: collectUFW(env), Nftables: collectNft(ctx, env)}
}

func collectUFW(env describeEnv) ufwSection {
	sec := ufwSection{Rules: []ufwRule{}}
	raw, err := env.readHostFile("/etc/ufw/ufw.conf")
	if err != nil {
		sec.Status, sec.Reason = classifyErr(err)
		return sec
	}
	sec.Status = statusOK
	for _, line := range strings.Split(string(raw), "\n") {
		if v, ok := strings.CutPrefix(strings.TrimSpace(line), "ENABLED="); ok {
			on := strings.EqualFold(strings.Trim(v, `"'`), "yes")
			sec.Enabled = &on
		}
	}
	readAny := false
	for _, f := range []string{"/etc/ufw/user.rules", "/etc/ufw/user6.rules"} {
		rules, err := env.readHostFile(f)
		if err != nil {
			if status, _ := classifyErr(err); status == statusNotVisible {
				sec.RulesStatus = statusNotVisible
			}
			continue
		}
		readAny = true
		for _, line := range strings.Split(string(rules), "\n") {
			rest, ok := strings.CutPrefix(strings.TrimSpace(line), "### tuple ###")
			if !ok {
				continue
			}
			// action proto dport dst sport src [direction] [comment=...]
			f := strings.Fields(rest)
			if len(f) < 6 || len(sec.Rules) >= maxFirewallRules {
				continue
			}
			r := ufwRule{Action: f[0], Proto: f[1], Port: f[2], From: f[5], Direction: "in"}
			if len(f) > 6 && !strings.HasPrefix(f[6], "comment=") {
				r.Direction = f[6]
			}
			sec.Rules = append(sec.Rules, r)
		}
	}
	switch {
	case readAny:
		sec.RulesStatus = statusOK
	case sec.RulesStatus == statusNotVisible:
		sec.Status, sec.Reason = statusPartial, "ufw's rules are "+notVisibleReason
	}
	return sec
}

func collectNft(ctx context.Context, env describeEnv) nftSection {
	sec := nftSection{Tables: []nftTable{}}
	out, err := env.run(ctx, "nft", "-j", "list", "ruleset")
	if err != nil {
		sec.Status, sec.Reason = classifyErr(err)
		return sec
	}
	var doc struct {
		Nftables []map[string]json.RawMessage `json:"nftables"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		sec.Status, sec.Reason = statusError, "nft answered something unreadable"
		return sec
	}
	type ref struct{ family, table, chain string }
	tables := map[ref]*nftTable{}
	chains := map[ref]*nftChain{}
	var order []ref
	for _, item := range doc.Nftables {
		var o struct {
			Family string `json:"family"`
			Table  string `json:"table"`
			Name   string `json:"name"`
			Chain  string `json:"chain"`
			Hook   string `json:"hook"`
			Policy string `json:"policy"`
		}
		switch {
		case item["table"] != nil:
			_ = json.Unmarshal(item["table"], &o)
			k := ref{o.Family, o.Name, ""}
			tables[k] = &nftTable{Family: o.Family, Name: o.Name, Chains: []nftChain{}}
			order = append(order, k)
		case item["chain"] != nil:
			_ = json.Unmarshal(item["chain"], &o)
			chains[ref{o.Family, o.Table, o.Name}] = &nftChain{Name: o.Name, Hook: o.Hook, Policy: o.Policy}
		case item["rule"] != nil:
			_ = json.Unmarshal(item["rule"], &o)
			if c := chains[ref{o.Family, o.Table, o.Chain}]; c != nil {
				c.Rules++
			}
		}
	}
	for _, k := range order {
		t := tables[k]
		for ck, c := range chains {
			if ck.family == k.family && ck.table == k.table {
				t.Chains = append(t.Chains, *c)
			}
		}
		sortChains(t.Chains)
		sec.Tables = append(sec.Tables, *t)
	}
	sec.Status = statusOK
	return sec
}

func sortChains(c []nftChain) {
	sort.Slice(c, func(i, j int) bool { return c[i].Name < c[j].Name })
}

type updatesSection struct {
	Status         string `json:"status"`
	Reason         string `json:"reason,omitempty"`
	Source         string `json:"source,omitempty"`
	AsOf           string `json:"as_of,omitempty"`
	Pending        *int   `json:"pending,omitempty"`
	Security       *int   `json:"security,omitempty"`
	RebootRequired bool   `json:"reboot_required"`
}

var (
	updatesPending  = regexp.MustCompile(`(\d+) updates? can be (applied|installed) immediately`)
	updatesSecurity = regexp.MustCompile(`(\d+) of these updates (are|is) (a )?(standard )?security updates?`)
)

func collectUpdates(ctx context.Context, env describeEnv) updatesSection {
	sec := updatesSection{}
	for _, f := range []string{"/run/reboot-required", "/var/run/reboot-required"} {
		if _, err := os.Stat(env.hostPath(f)); err == nil {
			sec.RebootRequired = true
		}
	}
	const notifier = "/var/lib/update-notifier/updates-available"
	if raw, err := env.readHostFile(notifier); err == nil {
		sec.Status, sec.Source = statusOK, notifier
		if st, err := os.Stat(env.hostPath(notifier)); err == nil {
			sec.AsOf = st.ModTime().UTC().Format(time.RFC3339)
		}
		zero := 0
		sec.Pending, sec.Security = &zero, &zero
		if m := updatesPending.FindStringSubmatch(string(raw)); m != nil {
			n, _ := strconv.Atoi(m[1])
			sec.Pending = &n
		}
		if m := updatesSecurity.FindStringSubmatch(string(raw)); m != nil {
			n, _ := strconv.Atoi(m[1])
			sec.Security = &n
		}
		return sec
	}
	// apt's own answer from its cache, simulated: changes nothing, needs no root.
	out, err := env.run(ctx, "apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade")
	if err != nil {
		sec.Status, sec.Reason = classifyErr(err)
		return sec
	}
	pending, security := 0, 0
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "Inst ") {
			pending++
			if strings.Contains(line, "-security") {
				security++
			}
		}
	}
	sec.Status, sec.Source = statusOK, "apt-get -s upgrade (from apt's cache)"
	sec.Pending, sec.Security = &pending, &security
	return sec
}

type timeSection struct {
	Status       string `json:"status"`
	Reason       string `json:"reason,omitempty"`
	NTP          *bool  `json:"ntp_enabled,omitempty"`
	Synchronized *bool  `json:"synchronized,omitempty"`
	Timezone     string `json:"timezone,omitempty"`
}

func collectTimeSync(ctx context.Context, env describeEnv) timeSection {
	out, err := env.run(ctx, "timedatectl", "show")
	if err != nil {
		status, reason := classifyErr(err)
		return timeSection{Status: status, Reason: reason}
	}
	sec := timeSection{Status: statusOK}
	for _, line := range strings.Split(out, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		yes := v == "yes"
		switch k {
		case "NTP":
			sec.NTP = &yes
		case "NTPSynchronized":
			sec.Synchronized = &yes
		case "Timezone":
			sec.Timezone = v
		}
	}
	return sec
}
