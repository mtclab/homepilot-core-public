package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
)

// App-layer replay protection (#362 slice 3), the Go counterpart of the hub's
// homepilot.agent_hub.replay. Defense-in-depth for the plaintext / untrusted-TLS
// deployment mode: every frame on a replay-protected connection carries a
// per-direction monotonic sequence and an HMAC-SHA256 keyed by the per-agent
// token, so a captured frame cannot be replayed, reordered, or altered.

// canonicalBytes serializes m (with the "mac" key removed) into the exact byte
// string the hub MACs: compact, sorted object keys, and HTML escaping OFF so
// '<', '>' and '&' — pervasive in shell commands — stay raw. This mirrors the
// hub's replay.canonical_bytes; the two MUST agree bit-for-bit or every frame
// fails. See replay_test.go / tests/test_agent_hub_replay.py for the pinned
// cross-language vector.
func canonicalBytes(m msg) ([]byte, error) {
	if _, ok := m["mac"]; ok {
		cp := make(msg, len(m))
		for k, v := range m {
			if k != "mac" {
				cp[k] = v
			}
		}
		m = cp
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(m); err != nil {
		return nil, err
	}
	// Encoder.Encode appends a trailing newline; the canonical form has none.
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// computeMAC returns the hex HMAC-SHA256 of canonicalBytes(m) under key.
func computeMAC(key []byte, m msg) (string, error) {
	cb, err := canonicalBytes(m)
	if err != nil {
		return "", err
	}
	h := hmac.New(sha256.New, key)
	h.Write(cb)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// verifyMAC constant-time compares mac against the expected HMAC of m.
func verifyMAC(key []byte, m msg, mac string) bool {
	want, err := computeMAC(key, m)
	if err != nil {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(want), []byte(mac)) == 1
}

// newNonceHex returns 16 random bytes as a 32-char hex string for register
// freshness.
func newNonceHex() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(b[:]), nil
}
