package main

import (
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Listening sockets (#818), from /proc/net - readable by every user - with the
// owning process NAME (/proc/PID/comm) and systemd unit (/proc/PID/cgroup).
// Never the command line. Mapping a socket to its process needs the process's
// fd table, which an unprivileged agent can read only for its own processes, so
// other owners are reported as not visible rather than guessed.

const maxSockets = 300

type listeningSocket struct {
	Proto   string `json:"proto"`
	Address string `json:"address"`
	Port    int    `json:"port"`
	User    string `json:"user,omitempty"`
	Process string `json:"process,omitempty"`
	Unit    string `json:"unit,omitempty"`
	Owner   string `json:"owner,omitempty"`
}

type socketsSection struct {
	Status  string            `json:"status"`
	Reason  string            `json:"reason,omitempty"`
	Sockets []listeningSocket `json:"sockets"`
}

type procSocket struct {
	proto string
	addr  string
	port  int
	uid   int
	inode string
}

func collectSockets(env describeEnv) socketsSection {
	var found []procSocket
	var firstErr error
	read := 0
	for _, proto := range []string{"tcp", "tcp6", "udp", "udp6"} {
		raw, err := env.readHostFile("/proc/net/" + proto)
		if err != nil {
			if firstErr == nil && !os.IsNotExist(err) {
				firstErr = err
			}
			continue
		}
		read++
		found = append(found, parseProcNet(proto, string(raw))...)
	}
	if read == 0 {
		status, reason := classifyErr(firstErr)
		if firstErr == nil {
			status, reason = statusNotVisible, "no /proc/net tables to read"
		}
		return socketsSection{Status: status, Reason: reason, Sockets: []listeningSocket{}}
	}

	owners := socketOwners(env)
	users := passwdNames(env)
	seen := map[string]bool{}
	out := []listeningSocket{}
	hidden := 0
	for _, s := range found {
		key := fmt.Sprintf("%s|%s|%d", s.proto, s.addr, s.port)
		if seen[key] {
			continue
		}
		seen[key] = true
		ls := listeningSocket{Proto: strings.TrimSuffix(s.proto, "6"), Address: s.addr, Port: s.port, User: users[s.uid]}
		if ls.User == "" {
			ls.User = strconv.Itoa(s.uid)
		}
		if o, ok := owners[s.inode]; ok {
			ls.Process, ls.Unit = o.process, o.unit
		} else {
			ls.Owner = "not visible to the agent"
			hidden++
		}
		out = append(out, ls)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Port != out[j].Port {
			return out[i].Port < out[j].Port
		}
		if out[i].Proto != out[j].Proto {
			return out[i].Proto < out[j].Proto
		}
		return out[i].Address < out[j].Address
	})
	if len(out) > maxSockets {
		out = out[:maxSockets]
	}
	sec := socketsSection{Status: statusOK, Sockets: out}
	if hidden > 0 {
		sec.Status = statusPartial
		sec.Reason = fmt.Sprintf("the owning process of %d socket(s) is not visible to the agent user (a privileged agent sees them)", hidden)
	}
	return sec
}

// parseProcNet reads one /proc/net/{tcp,udp}[6] table: listening TCP sockets
// (state 0A) and bound UDP sockets with no peer.
func parseProcNet(proto, text string) []procSocket {
	var out []procSocket
	for i, line := range strings.Split(text, "\n") {
		f := strings.Fields(line)
		if i == 0 || len(f) < 10 {
			continue
		}
		state := f[3]
		if strings.HasPrefix(proto, "tcp") && state != "0A" {
			continue
		}
		if strings.HasPrefix(proto, "udp") && !strings.HasSuffix(f[2], ":0000") {
			continue
		}
		addr, port, ok := parseHexAddr(f[1])
		if !ok {
			continue
		}
		uid, _ := strconv.Atoi(f[7])
		out = append(out, procSocket{proto: proto, addr: addr, port: port, uid: uid, inode: f[9]})
	}
	return out
}

// parseHexAddr decodes "0100007F:0035" (IPv4) or the 32-hex-digit IPv6 form.
// The kernel prints each 32-bit word in host (little-endian) order.
func parseHexAddr(s string) (string, int, bool) {
	hostHex, portHex, ok := strings.Cut(s, ":")
	if !ok {
		return "", 0, false
	}
	port, err := strconv.ParseUint(portHex, 16, 16)
	if err != nil {
		return "", 0, false
	}
	raw, err := hex.DecodeString(hostHex)
	if err != nil || (len(raw) != 4 && len(raw) != 16) {
		return "", 0, false
	}
	ip := make(net.IP, len(raw))
	for w := 0; w < len(raw); w += 4 {
		ip[w], ip[w+1], ip[w+2], ip[w+3] = raw[w+3], raw[w+2], raw[w+1], raw[w]
	}
	return ip.String(), int(port), true
}

type socketOwner struct {
	process string
	unit    string
}

// socketOwners maps socket inodes to the process (and unit) holding them, for
// every process whose fd table the agent can read.
func socketOwners(env describeEnv) map[string]socketOwner {
	out := map[string]socketOwner{}
	procDir := env.hostPath("/proc")
	entries, err := os.ReadDir(procDir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		pid := e.Name()
		if _, err := strconv.Atoi(pid); err != nil {
			continue
		}
		fds, err := os.ReadDir(filepath.Join(procDir, pid, "fd"))
		if err != nil {
			continue
		}
		var owner *socketOwner
		for _, fd := range fds {
			target, err := os.Readlink(filepath.Join(procDir, pid, "fd", fd.Name()))
			if err != nil || !strings.HasPrefix(target, "socket:[") {
				continue
			}
			inode := strings.TrimSuffix(strings.TrimPrefix(target, "socket:["), "]")
			if owner == nil {
				owner = &socketOwner{process: procComm(env, pid), unit: procUnit(env, pid)}
			}
			if _, done := out[inode]; !done {
				out[inode] = *owner
			}
		}
	}
	return out
}

// procComm is the process NAME (comm), never its argv.
func procComm(env describeEnv, pid string) string {
	b, err := env.readHostFile("/proc/" + pid + "/comm")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// procUnit is the systemd unit (or container scope) a process belongs to,
// from its cgroup path.
func procUnit(env describeEnv, pid string) string {
	b, err := env.readHostFile("/proc/" + pid + "/cgroup")
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(b), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 {
			continue
		}
		segs := strings.Split(parts[2], "/")
		for i := len(segs) - 1; i >= 0; i-- {
			seg := segs[i]
			if strings.HasPrefix(seg, "docker-") && strings.HasSuffix(seg, ".scope") {
				id := strings.TrimSuffix(strings.TrimPrefix(seg, "docker-"), ".scope")
				if len(id) > 12 {
					id = id[:12]
				}
				return "docker container " + id
			}
			if strings.HasSuffix(seg, ".service") || strings.HasSuffix(seg, ".scope") {
				return seg
			}
		}
	}
	return ""
}

// passwdNames maps uids to user names from the host's /etc/passwd.
func passwdNames(env describeEnv) map[int]string {
	out := map[int]string{}
	b, err := env.readHostFile("/etc/passwd")
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		f := strings.Split(line, ":")
		if len(f) < 3 {
			continue
		}
		if uid, err := strconv.Atoi(f[2]); err == nil {
			out[uid] = f[0]
		}
	}
	return out
}
