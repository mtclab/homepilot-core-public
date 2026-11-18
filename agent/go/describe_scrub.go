package main

import (
	"regexp"
	"strings"
)

// The host description's deny filter (#818). It runs over the FINISHED document
// in the agent, after every collector, so a secret that slipped past a
// collector's own field allowlist still never leaves the host.
//
// It works on values, not on where they came from: a string that holds a
// private-key block, a bearer/basic credential, a well-known token format, a JWT
// or a "password=..."-style assignment is replaced whole by redactedMark; URL
// userinfo ("https://user:pass@host") loses only the credentials, so the target
// stays readable. A {"name": ..., "value": ...} pair whose name looks like a
// credential (a header, a label) loses its value. Every string is capped at
// describeFieldCap.

const redactedMark = "[redacted]"

var (
	// secretValuePatterns redact the WHOLE string they match in.
	secretValuePatterns = []*regexp.Regexp{
		regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*PRIVATE KEY|PRIVATE KEY-----`),
		regexp.MustCompile(`(?i)\b(bearer|basic|token|digest)\s+[A-Za-z0-9._~+/=-]{8,}`),
		regexp.MustCompile(`(?i)(passw(or)?d|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|auth[_-]?key)s?["']?\s*[:=]\s*\S`),
		// Well-known token formats: GitHub, GitLab, Slack, AWS access keys,
		// OpenAI-style keys, HomePilot's own tokens.
		regexp.MustCompile(`\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|hp_[0-9a-f]{13})\b`),
		// A JSON Web Token.
		regexp.MustCompile(`eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`),
	}
	// urlUserinfo matches the credentials part of a URL.
	urlUserinfo = regexp.MustCompile(`([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@`)
	// secretName is a header/label/directive name whose value is a credential.
	secretName = regexp.MustCompile(`(?i)(key|secret|token|passw|pwd|credential|cookie|authorization|auth|session|signature)`)
)

type scrubber struct {
	redacted int
}

// walk scrubs a JSON tree in place and returns it.
func (s *scrubber) walk(v any) any {
	switch t := v.(type) {
	case map[string]any:
		if name, ok := t["name"].(string); ok && secretName.MatchString(name) {
			if val, has := t["value"]; has && val != nil && val != "" {
				t["value"] = redactedMark
				s.redacted++
			}
		}
		for k, x := range t {
			t[k] = s.walk(x)
		}
		return t
	case []any:
		for i, x := range t {
			t[i] = s.walk(x)
		}
		return t
	case string:
		return s.str(t)
	default:
		return v
	}
}

// str scrubs one string value.
func (s *scrubber) str(v string) string {
	if v == redactedMark {
		return v
	}
	if urlUserinfo.MatchString(v) {
		v = urlUserinfo.ReplaceAllString(v, "${1}"+redactedMark+"@")
		s.redacted++
	}
	for _, rx := range secretValuePatterns {
		if rx.MatchString(v) {
			s.redacted++
			return redactedMark
		}
	}
	return capField(v)
}

// capField bounds one string at describeFieldCap bytes, on a rune boundary.
func capField(v string) string {
	if len(v) <= describeFieldCap {
		return v
	}
	cut := describeFieldCap
	for cut > 0 && !utf8RuneStart(v[cut]) {
		cut--
	}
	return v[:cut] + "...[truncated]"
}

func utf8RuneStart(b byte) bool { return b&0xC0 != 0x80 }

// looksSecretName reports whether a directive, header or label name names a
// credential. Collectors use it to never even pick such a value up.
func looksSecretName(name string) bool {
	return secretName.MatchString(strings.TrimSpace(name))
}
