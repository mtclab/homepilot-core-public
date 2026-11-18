package main

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// systemd unit files at the read tier (#853).
//
// Unit files and drop-ins are readable (they say how a host is built), but they
// also carry other services' secrets: `Environment=TOKEN=...` in a drop-in,
// `SetCredential=`, a WireGuard .netdev's `PrivateKey=`. And a unit names the
// files it loads secrets from (`EnvironmentFile=`, `LoadCredential=`), which
// were readable whenever they sat under a read prefix and their name did not
// say "env" or "secret".
//
// Two rules, both enforced HERE so a secret never leaves the host:
//
//   - every unit-shaped file is returned with those values redacted
//     (redactUnitText); the variable NAMES stay, so the file still explains the
//     service;
//   - any file a unit on this host names as a secret source is refused
//     (unitSecretSourceRefusal), wherever it lives.
//
// The rule set is the shared policy's "unit_files" section. The control plane
// mirrors the rewrite (src/homepilot/adapters/readonly_policy.py) and the
// corpus (testdata/readonly_policy_corpus.json, "unit_redaction") pins both to
// the same bytes.

type policyUnitFiles struct {
	Dirs               []string `json:"dirs"`
	UnitBasenameRegex  string   `json:"unit_basename_regex"`
	DropinDirRegex     string   `json:"dropin_dir_regex"`
	EnvKeys            []string `json:"env_keys"`
	CredentialKeys     []string `json:"credential_keys"`
	SecretKeys         []string `json:"secret_keys"`
	ExecKeys           []string `json:"exec_keys"`
	FileKeys           []string `json:"file_keys"`
	CredentialFileKeys []string `json:"credential_file_keys"`
	Mark               string   `json:"mark"`
	ArgsMark           string   `json:"args_mark"`
}

type compiledUnitFiles struct {
	dirs               []string
	unitBasename       *regexp.Regexp
	dropinDir          *regexp.Regexp
	envKeys            map[string]bool
	credentialKeys     map[string]bool
	secretKeys         map[string]bool
	execKeys           map[string]bool
	fileKeys           map[string]bool
	credentialFileKeys map[string]bool
	mark               string
	argsMark           string
}

var unitPolicy compiledUnitFiles

func init() {
	var p struct {
		UnitFiles policyUnitFiles `json:"unit_files"`
	}
	if err := json.Unmarshal(readonlyPolicyJSON, &p); err != nil {
		panic(fmt.Sprintf("embedded readonly_policy.json is invalid: %v", err))
	}
	u := p.UnitFiles
	if u.Mark == "" || u.ArgsMark == "" || len(u.Dirs) == 0 || len(u.EnvKeys) == 0 || len(u.ExecKeys) == 0 {
		panic("embedded readonly_policy.json has no unit_files section")
	}
	unitPolicy = compiledUnitFiles{
		dirs:               u.Dirs,
		unitBasename:       wholeMatch(u.UnitBasenameRegex),
		dropinDir:          wholeMatch(u.DropinDirRegex),
		envKeys:            lowerSet(u.EnvKeys),
		credentialKeys:     lowerSet(u.CredentialKeys),
		secretKeys:         lowerSet(u.SecretKeys),
		execKeys:           lowerSet(u.ExecKeys),
		fileKeys:           lowerSet(u.FileKeys),
		credentialFileKeys: lowerSet(u.CredentialFileKeys),
		mark:               u.Mark,
		argsMark:           u.ArgsMark,
	}
}

func lowerSet(items []string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, s := range items {
		out[strings.ToLower(s)] = true
	}
	return out
}

// isUnitShaped reports whether a path is a systemd unit, drop-in or systemd
// configuration file: under a systemd directory (or one HP_AGENT_UNIT_DIRS
// names), named like a unit, or inside a unit's drop-in directory
// (app.service.d/override.conf).
func isUnitShaped(p string) bool {
	if underAnyPrefix(p, unitScanRoots()) {
		return true
	}
	return unitPolicy.unitBasename.MatchString(filepath.Base(p)) ||
		unitPolicy.dropinDir.MatchString(filepath.Base(filepath.Dir(p)))
}

// unitLine is one LOGICAL line of a unit file: the physical lines it spans (a
// trailing backslash continues a line; comment lines inside a continuation are
// skipped by systemd and kept here), and, for an assignment, its key, the text
// up to and including '=' and the joined value. A commented-out assignment
// ("#Environment=TOKEN=old") is parsed too: systemd ignores it, but the old
// value is still in the file.
type unitLine struct {
	physical []string
	name     string // the key as written
	key      string // lower-cased, for matching
	prefix   string
	value    string
}

var unitKeyRe = regexp.MustCompile(`^([ \t]*(?:[#;][#; \t]*)?([A-Za-z][A-Za-z0-9]*)[ \t]*=)(.*)$`)

func isUnitComment(line string) bool {
	t := strings.TrimLeft(line, " \t")
	return strings.HasPrefix(t, "#") || strings.HasPrefix(t, ";")
}

func continuesLine(line string) bool {
	return strings.HasSuffix(strings.TrimRight(line, "\r"), `\`)
}

// unitLogicalLines splits a unit file into logical lines, exactly as the
// control plane's mirror does.
func unitLogicalLines(text string) []unitLine {
	lines := strings.Split(text, "\n")
	var out []unitLine
	for i := 0; i < len(lines); {
		group := []string{lines[i]}
		if !isUnitComment(lines[i]) && continuesLine(lines[i]) {
			for i+len(group) < len(lines) {
				next := lines[i+len(group)]
				group = append(group, next)
				if !isUnitComment(next) && !continuesLine(next) {
					break
				}
			}
		}
		i += len(group)
		ul := unitLine{physical: group}
		var joined strings.Builder
		for j, l := range group {
			if j > 0 && isUnitComment(l) {
				continue
			}
			l = strings.TrimRight(l, "\r")
			if strings.HasSuffix(l, `\`) {
				l = l[:len(l)-1] + " "
			}
			joined.WriteString(l)
		}
		if m := unitKeyRe.FindStringSubmatch(joined.String()); m != nil {
			ul.prefix, ul.name, ul.key, ul.value = m[1], m[2], strings.ToLower(m[2]), m[3]
		}
		out = append(out, ul)
	}
	return out
}

var envNameRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
var credentialIDRe = regexp.MustCompile(`^[A-Za-z0-9_.@-]+$`)

// unitWord is one word of an Environment= value: its unquoted text and the
// quote character it opened with (0 when it did not start quoted).
type unitWord struct {
	text  string
	quote byte
}

// unitWords splits an Environment= value into words the way systemd does:
// whitespace separates words outside quotes, "..." and '...' group, and a
// backslash takes the next character literally (except inside '...').
func unitWords(value string) []unitWord {
	var words []unitWord
	var cur strings.Builder
	var quote, opened byte
	inWord := false
	for i := 0; i < len(value); i++ {
		c := value[i]
		if quote == 0 {
			switch {
			case c == ' ' || c == '\t' || c == '\r':
				if inWord {
					words = append(words, unitWord{cur.String(), opened})
					cur.Reset()
					inWord, opened = false, 0
				}
			case c == '"' || c == '\'':
				if !inWord {
					opened = c
				}
				inWord = true
				quote = c
			case c == '\\':
				inWord = true
				if i+1 < len(value) {
					i++
					cur.WriteByte(value[i])
				}
			default:
				inWord = true
				cur.WriteByte(c)
			}
			continue
		}
		switch {
		case c == quote:
			quote = 0
		case c == '\\' && quote == '"' && i+1 < len(value):
			i++
			cur.WriteByte(value[i])
		default:
			cur.WriteByte(c)
		}
	}
	if inWord {
		words = append(words, unitWord{cur.String(), opened})
	}
	return words
}

// redactEnvWord keeps a word's variable name and replaces its value. A word
// with no valid name is replaced whole: what precedes '=' is then not a name
// and may itself be the secret.
func redactEnvWord(w unitWord) string {
	out := unitPolicy.mark
	if name, _, ok := strings.Cut(w.text, "="); ok && envNameRe.MatchString(name) {
		out = name + "=" + unitPolicy.mark
	}
	if w.quote != 0 {
		return string(w.quote) + out + string(w.quote)
	}
	return out
}

// execPrefixChars are the special prefixes systemd allows before an Exec*=
// program ("-" ignore failure, "@" argv0 follows, ":" no substitution, "+" "!"
// privilege handling, "|" run through the user's shell).
const execPrefixChars = "-@:+!|"

// redactExec keeps an Exec*= value's prefix characters and program and drops
// every argument (#853, the #817 class: argv carries tokens). It reports
// whether there was anything to drop; a bare program is left as written.
func redactExec(value string) (string, bool) {
	rest := strings.TrimLeft(value, execPrefixChars)
	prefix := value[:len(value)-len(rest)]
	words := unitWords(rest)
	if len(words) < 2 {
		return value, false
	}
	program := words[0].text
	if words[0].quote != 0 || strings.ContainsAny(program, " \t\"'\\") {
		program = `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(program) + `"`
	}
	return prefix + program + " " + unitPolicy.argsMark, true
}

// redactUnitText rewrites a unit-shaped file's secret-bearing assignments and
// returns the text and how many values it replaced. A redacted logical line is
// written back as ONE physical line; every other line is kept byte for byte.
// The rewrite is a fixed point: redacting its own output changes nothing, which
// is how the control plane tells a redacted read from the host's real bytes.
func redactUnitText(text string) (string, int) {
	count := 0
	var out []string
	for _, ul := range unitLogicalLines(text) {
		value := strings.Trim(ul.value, " \t\r")
		switch {
		case unitPolicy.envKeys[ul.key]:
			words := unitWords(value)
			red := make([]string, 0, len(words))
			for _, w := range words {
				red = append(red, redactEnvWord(w))
			}
			count += len(words)
			out = append(out, ul.prefix+strings.Join(red, " "))
		case unitPolicy.credentialKeys[ul.key]:
			if value == "" {
				out = append(out, ul.prefix)
				continue
			}
			count++
			if id, _, ok := strings.Cut(value, ":"); ok && credentialIDRe.MatchString(id) {
				out = append(out, ul.prefix+id+":"+unitPolicy.mark)
			} else {
				out = append(out, ul.prefix+unitPolicy.mark)
			}
		case unitPolicy.secretKeys[ul.key]:
			if value == "" {
				out = append(out, ul.prefix)
				continue
			}
			count++
			out = append(out, ul.prefix+unitPolicy.mark)
		case unitPolicy.execKeys[ul.key]:
			line, redacted := redactExec(value)
			if redacted {
				count++
				out = append(out, ul.prefix+line)
			} else {
				out = append(out, ul.physical...)
			}
		default:
			// Kept byte for byte, except a commented-out assignment inside a
			// continuation, which is redacted as the line it is.
			out = append(out, ul.physical[0])
			for _, l := range ul.physical[1:] {
				if isUnitComment(l) {
					r, n := redactUnitText(l)
					count += n
					l = r
				}
				out = append(out, l)
			}
		}
	}
	return strings.Join(out, "\n"), count
}

// --- secret sources: files a unit names -------------------------------------

// maxUnitScanFileBytes bounds what the scan reads per file. A unit or drop-in
// is a few KiB; anything larger is not one.
const maxUnitScanFileBytes = 1 << 20

// secretSource is one file (or directory) a unit loads secrets from: an exact
// path, or a pattern when the unit wrote it with % specifiers.
type secretSource struct {
	unit    string
	key     string // as the unit wrote it, for the refusal
	path    string
	pattern *regexp.Regexp
}

func (s secretSource) covers(p string) bool {
	if s.pattern != nil {
		return s.pattern.MatchString(p)
	}
	return p == s.path || strings.HasPrefix(p, strings.TrimRight(s.path, "/")+"/")
}

// unitScanRoots are the directories whose units are scanned: the policy's
// systemd directories plus any an operator keeps units in
// (HP_AGENT_UNIT_DIRS, colon-separated). The variable can only ADD roots.
func unitScanRoots() []string {
	roots := append([]string(nil), unitPolicy.dirs...)
	if v := os.Getenv("HP_AGENT_UNIT_DIRS"); v != "" {
		roots = append(roots, splitPrefixes(v)...)
	}
	return roots
}

// specifierPattern turns a unit path holding % specifiers into a pattern in
// which each specifier matches anything ("%%" is a literal percent sign).
func specifierPattern(p string) *regexp.Regexp {
	var b strings.Builder
	b.WriteString("^")
	for i := 0; i < len(p); i++ {
		if p[i] == '%' && i+1 < len(p) {
			i++
			if p[i] == '%' {
				b.WriteString("%")
			} else {
				b.WriteString(".*")
			}
			continue
		}
		b.WriteString(regexp.QuoteMeta(string(p[i])))
	}
	b.WriteString("$")
	return regexp.MustCompile(b.String())
}

// sourcesIn lists the secret sources one unit file names.
func sourcesIn(unit, text string) []secretSource {
	var out []secretSource
	for _, ul := range unitLogicalLines(text) {
		value := strings.Trim(ul.value, " \t\r")
		var path string
		switch {
		case unitPolicy.fileKeys[ul.key]:
			path = strings.TrimPrefix(value, "-")
		case unitPolicy.credentialFileKeys[ul.key]:
			// LoadCredential=ID:PATH. With no PATH the credential comes from the
			// credential store, which the read policy already denies.
			_, path, _ = strings.Cut(value, ":")
		default:
			continue
		}
		path = strings.Trim(path, " \t\r")
		if !strings.HasPrefix(path, "/") {
			continue
		}
		src := secretSource{unit: unit, key: ul.name}
		if strings.Contains(path, "%") {
			src.pattern = specifierPattern(path)
			out = append(out, src)
			continue
		}
		src.path = filepath.Clean(path)
		out = append(out, src)
		if r, err := filepath.EvalSymlinks(src.path); err == nil && r != src.path {
			alias := src
			alias.path = r
			out = append(out, alias)
		}
	}
	return out
}

// unitSecretSources walks the unit directories and collects every secret
// source their units and drop-ins name. Symlinked units (a .wants/ link, a unit
// linked in from elsewhere) are followed to their target. Unreadable entries
// are skipped: a unit the agent cannot read is a unit it cannot serve either.
func unitSecretSources() []secretSource {
	var out []secretSource
	seen := map[string]bool{}
	for _, root := range unitScanRoots() {
		resolved, err := filepath.EvalSymlinks(strings.TrimRight(root, "/"))
		if err != nil || seen[resolved] {
			continue
		}
		seen[resolved] = true
		_ = filepath.WalkDir(resolved, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				if d != nil && d.IsDir() {
					return fs.SkipDir
				}
				return nil
			}
			// Only a unit or a drop-in can name a secret source; the systemd
			// directories also hold binaries and generators, which are skipped.
			if d.IsDir() || !(unitPolicy.unitBasename.MatchString(d.Name()) ||
				unitPolicy.dropinDir.MatchString(filepath.Base(filepath.Dir(p)))) {
				return nil
			}
			info, err := os.Stat(p)
			if err != nil || !info.Mode().IsRegular() || info.Size() > maxUnitScanFileBytes {
				return nil
			}
			data, err := os.ReadFile(p)
			if err != nil {
				return nil
			}
			out = append(out, sourcesIn(p, string(data))...)
			return nil
		})
	}
	return out
}

// unitSecretSourceRefusal says why a path may not be read because a unit on
// this host loads secrets from it, or "" when none does. Both the path as given
// and its resolved target are checked.
func unitSecretSourceRefusal(paths ...string) string {
	for _, src := range unitSecretSources() {
		for _, p := range paths {
			if src.covers(p) {
				return fmt.Sprintf("%s is a secret source: %s names it in %s= (it holds that "+
					"service's environment or credentials, which the read tier never shows)",
					p, src.unit, src.key)
			}
		}
	}
	return ""
}
