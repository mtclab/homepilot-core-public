package main

// Host description (#818): a structured, versioned, read-only account of how
// this machine is built - what listens, which units and containers run, how the
// web server routes, which certificates it serves, the firewall, pending updates
// and time sync - so the knowledge base can hold a rebuild recipe for it.
//
// Three rules shape every collector in describe_*.go:
//
//   - Read-only and unprivileged first. The agent usually runs as hp-agent, so
//     each part reports what that user can see, and says "not visible to the
//     agent" for what it cannot, instead of failing the whole description.
//   - Allowlisted output. Each part emits a FIXED set of fields. Nothing is
//     passed through wholesale: no environments, no command lines, no container
//     commands, no header values outside a known-safe list, nothing under a
//     key/secret/token/password-looking directive.
//   - Secrets never leave the host. scrubDescription runs over the finished
//     document as the last step, whatever the collectors did: private-key
//     blocks, bearer/basic credentials, URL userinfo, key=value secrets and
//     well-known token formats are redacted here, in the agent.
//
// Every string has a size cap and so has the whole document, so a description
// always fits one hub frame (see maxPayloadBytes).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// describeSchema versions the document. The hub refuses a schema it does not
// know rather than storing something its renderers would misread.
const describeSchema = "hp.host-description/1"

const (
	// describeDocCap bounds the whole serialized document: half the payload
	// budget, so the frame around it always fits.
	describeDocCap = maxPayloadBytes / 2
	// describeFieldCap bounds every string in the document.
	describeFieldCap = 512
	// describeFileCap bounds any one file a collector reads.
	describeFileCap = 1 << 20
	// describeCommandTimeout bounds each command a collector runs, and
	// describeTotalTimeout the whole description.
	describeCommandTimeout = 10 * time.Second
	describeTotalTimeout   = 45 * time.Second
)

// Section statuses. "partial" means the section is there but the agent user
// could not see all of it (the reason says what).
const (
	statusOK           = "ok"
	statusPartial      = "partial"
	statusNotVisible   = "not_visible"
	statusNotInstalled = "not_installed"
	statusError        = "error"
)

// notVisibleReason is the one sentence for "the agent user cannot read this".
const notVisibleReason = "not visible to the agent: its user cannot read this (a privileged agent can)"

// describeEnv is everything a collector touches on the host, so tests can point
// it at a fixture tree and canned command output.
type describeEnv struct {
	// root is the filesystem the description reads ("/" on a host).
	root string
	// run runs one fixed command (never anything the hub sent) and returns its
	// stdout. Errors are classified by classifyErr.
	run func(ctx context.Context, name string, args ...string) (string, error)
	// docker GETs one Docker Engine API path over the local socket.
	docker func(ctx context.Context, path string) ([]byte, error)
	// euid is the agent's effective uid: 0 means it can see everything.
	euid       int
	privileged bool
	now        func() time.Time
}

// hostPath maps an absolute host path into the env's root.
func (e describeEnv) hostPath(p string) string {
	return filepath.Join(e.root, filepath.FromSlash(p))
}

// readHostFile reads a host file, capped at describeFileCap.
func (e describeEnv) readHostFile(p string) ([]byte, error) {
	f, err := os.Open(e.hostPath(p))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(io.LimitReader(f, describeFileCap))
}

// errNotInstalled is returned by run for a command that is not on the host.
var errNotInstalled = errors.New("not installed")

// cmdError is a command that ran and failed; stderr is kept only to classify
// the failure, never put in the document.
type cmdError struct {
	code   int
	stderr string
}

func (e *cmdError) Error() string { return fmt.Sprintf("exit status %d", e.code) }

var permissionWords = regexp.MustCompile(`(?i)permission denied|operation not permitted|must be root|need to be root|access denied|not authorized|interactive authentication required`)

// classifyErr turns a read or command error into a section status + reason.
func classifyErr(err error) (string, string) {
	var ce *cmdError
	switch {
	case err == nil:
		return statusOK, ""
	case errors.Is(err, errNotInstalled), errors.Is(err, os.ErrNotExist):
		return statusNotInstalled, "not installed on this host"
	case errors.Is(err, os.ErrPermission):
		return statusNotVisible, notVisibleReason
	case errors.As(err, &ce) && permissionWords.MatchString(ce.stderr):
		return statusNotVisible, notVisibleReason
	case errors.Is(err, context.DeadlineExceeded):
		return statusError, "timed out"
	case errors.As(err, &ce):
		return statusError, fmt.Sprintf("the command failed (exit status %d)", ce.code)
	default:
		return statusError, "could not be read"
	}
}

// describeCommandRunner runs a fixed command found under root's standard bin
// directories, with a minimal environment and capped output.
func describeCommandRunner(root string) func(ctx context.Context, name string, args ...string) (string, error) {
	return func(ctx context.Context, name string, args ...string) (string, error) {
		bin := ""
		for _, dir := range []string{"usr/bin", "usr/sbin", "bin", "sbin"} {
			p := filepath.Join(root, dir, name)
			if st, err := os.Stat(p); err == nil && !st.IsDir() && st.Mode()&0o111 != 0 {
				bin = p
				break
			}
		}
		if bin == "" {
			return "", errNotInstalled
		}
		cctx, cancel := context.WithTimeout(ctx, describeCommandTimeout)
		defer cancel()
		cmd := exec.CommandContext(cctx, bin, args...)
		cmd.Env = []string{"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=C", "LC_ALL=C", "SYSTEMD_PAGER=", "SYSTEMD_COLORS=0"}
		stdout := &capWriter{limit: 4 * describeFileCap}
		stderr := &capWriter{limit: 4096}
		cmd.Stdout, cmd.Stderr = stdout, stderr
		err := cmd.Run()
		if cctx.Err() != nil {
			return "", context.DeadlineExceeded
		}
		if err != nil {
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				return string(stdout.buf), &cmdError{code: ee.ExitCode(), stderr: string(stderr.buf)}
			}
			return "", err
		}
		return string(stdout.buf), nil
	}
}

// describeRoot is the filesystem the description reads. "/" on a host; a test
// (or a host whose root is mounted elsewhere) points it at another tree.
func describeRoot() string {
	if v := strings.TrimSpace(os.Getenv("HP_AGENT_DESCRIBE_ROOT")); v != "" {
		return v
	}
	return "/"
}

func defaultDescribeEnv(privileged bool) describeEnv {
	root := describeRoot()
	return describeEnv{
		root:       root,
		run:        describeCommandRunner(root),
		docker:     dockerClient(root),
		euid:       os.Geteuid(),
		privileged: privileged,
		now:        time.Now,
	}
}

// describeHost collects the whole description, scrubs it and fits it to the
// cap. It never fails: a part that cannot be read says so in its status.
func describeHost(ctx context.Context, env describeEnv) map[string]any {
	ctx, cancel := context.WithTimeout(ctx, describeTotalTimeout)
	defer cancel()
	started := env.now()
	return finishDescription(collectDescription(ctx, env), env.now().Sub(started))
}

// collectDescription runs every collector and returns the RAW tree, before the
// deny filter. Nothing may send this tree anywhere: finishDescription is the
// only way out of the agent.
func collectDescription(ctx context.Context, env describeEnv) map[string]any {
	nginx := collectNginx(env)
	doc := map[string]any{
		"schema":       describeSchema,
		"collected_at": env.now().UTC().Format(time.RFC3339),
		"agent": map[string]any{
			"version":    agentVersion(),
			"privileged": env.privileged,
			"run_as":     runAsName(env.euid),
		},
		"host":         collectHostIdentity(env),
		"listening":    collectSockets(env),
		"units":        collectUnits(ctx, env),
		"docker":       collectDocker(ctx, env),
		"nginx":        nginx,
		"certificates": collectCertificates(env, nginx.certificatePaths()),
		"firewall":     collectFirewall(ctx, env),
		"updates":      collectUpdates(ctx, env),
		"time":         collectTimeSync(ctx, env),
	}
	tree, _ := toTree(doc).(map[string]any)
	return tree
}

// finishDescription applies the deny filter and the size cap.
func finishDescription(tree map[string]any, took time.Duration) map[string]any {
	s := &scrubber{}
	s.walk(tree)
	tree["redactions"] = s.redacted
	tree["duration_ms"] = took.Milliseconds()
	fitToCap(tree, describeDocCap)
	return tree
}

// toTree turns the typed sections into a plain JSON tree, the one shape the
// scrubber and the cap work on.
func toTree(v any) any {
	raw, err := json.Marshal(v)
	if err != nil {
		return map[string]any{"schema": describeSchema, "error": "could not encode the description"}
	}
	var out any
	_ = json.Unmarshal(raw, &out)
	return out
}

func runAsName(uid int) string {
	if u, err := user.LookupId(strconv.Itoa(uid)); err == nil {
		return u.Username
	}
	return strconv.Itoa(uid)
}

type hostIdentity struct {
	Hostname string `json:"hostname"`
	OS       string `json:"os,omitempty"`
	Kernel   string `json:"kernel,omitempty"`
}

func collectHostIdentity(env describeEnv) hostIdentity {
	id := hostIdentity{}
	if b, err := env.readHostFile("/etc/hostname"); err == nil {
		id.Hostname = strings.TrimSpace(string(b))
	}
	if id.Hostname == "" && env.root == "/" {
		id.Hostname, _ = os.Hostname()
	}
	if b, err := env.readHostFile("/etc/os-release"); err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if v, ok := strings.CutPrefix(line, "PRETTY_NAME="); ok {
				id.OS = strings.Trim(v, `"'`)
			}
		}
	}
	if b, err := env.readHostFile("/proc/sys/kernel/osrelease"); err == nil {
		id.Kernel = strings.TrimSpace(string(b))
	}
	return id
}

// fitToCap trims the document until it serializes within limit: the longest
// list is halved, repeatedly, and each cut is named under "truncated", so a
// reader knows the description is incomplete and where.
func fitToCap(doc map[string]any, limit int) {
	var cuts []string
	for i := 0; i < 64; i++ {
		raw, _ := json.Marshal(doc)
		if len(raw) <= limit {
			break
		}
		ref := largestList(doc)
		if ref == nil {
			// Nothing left to trim: drop the sections wholesale, biggest first.
			dropLargestSection(doc)
			cuts = append(cuts, "a whole section (the document was still too large)")
			continue
		}
		keep := len(ref.list) / 2
		cuts = append(cuts, fmt.Sprintf("%s: kept %d of %d", ref.path, keep, len(ref.list)))
		ref.set(ref.list[:keep])
	}
	if len(cuts) > 0 {
		sort.Strings(cuts)
		doc["truncated"] = cuts
	}
}

// listRef is one list in the document tree, with a way to replace it in its
// parent.
type listRef struct {
	path string
	list []any
	set  func([]any)
}

func collectLists(node any, path string, set func([]any), out *[]listRef) {
	switch t := node.(type) {
	case map[string]any:
		for k, v := range t {
			parent, key := t, k
			collectLists(v, path+"."+key, func(nv []any) { parent[key] = nv }, out)
		}
	case []any:
		*out = append(*out, listRef{path: strings.TrimPrefix(path, "."), list: t, set: set})
		for i, v := range t {
			parent, idx := t, i
			collectLists(v, fmt.Sprintf("%s[%d]", path, idx), func(nv []any) { parent[idx] = nv }, out)
		}
	}
}

// largestList is the non-empty list with the largest serialized size.
func largestList(doc map[string]any) *listRef {
	var refs []listRef
	collectLists(doc, "", func([]any) {}, &refs)
	var best *listRef
	bestSize := -1
	for i := range refs {
		if len(refs[i].list) == 0 {
			continue
		}
		raw, _ := json.Marshal(refs[i].list)
		if len(raw) > bestSize {
			bestSize = len(raw)
			best = &refs[i]
		}
	}
	return best
}

// dropLargestSection removes the largest top-level section, keeping the
// document's identity fields.
func dropLargestSection(doc map[string]any) {
	keep := map[string]bool{"schema": true, "collected_at": true, "agent": true, "host": true, "truncated": true}
	bestKey, bestSize := "", -1
	for k, v := range doc {
		if keep[k] {
			continue
		}
		raw, _ := json.Marshal(v)
		if len(raw) > bestSize {
			bestKey, bestSize = k, len(raw)
		}
	}
	if bestKey != "" {
		doc[bestKey] = map[string]any{"status": statusError, "reason": "dropped: the description exceeded its size cap"}
	}
}
