package main

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const maxMessageSize = 1 << 20 // 1 MiB, matches the hub

// maxPayloadBytes is how much VARIABLE payload (a file's contents, a command's
// stdout) one frame may carry. The remainder of maxMessageSize is headroom for
// the frame envelope, the seq/mac fields, and JSON escaping - a byte of content
// can cost up to six bytes on the wire ("\u0000"), so a budget measured on the
// raw bytes needs slack behind it.
//
// This bound exists because an oversize frame is not a recoverable error on the
// wire. The hub refuses to parse a body it cannot MAC-verify, so on a
// replay-protected connection - which is every agent holding a per-agent
// credential, i.e. the steady state - an oversize reply CLOSES THE CONNECTION.
// A single `cat /var/log/syslog` therefore knocked the host off the hub and the
// operator got no answer at all. The agent must never produce a frame the hub
// cannot accept; refusing with a sentence beats disconnecting the fleet.
const maxPayloadBytes = 512 * 1024

// errFrameTooLarge is returned by encodeMessage for a frame that exceeds the
// wire limit. It is the last-resort guard behind the per-payload budgets: no
// path may put an unsendable frame on the socket.
var errFrameTooLarge = errors.New("frame exceeds the hub's maximum message size")

// protocolVersion is sent as "v" in the register frame so the hub can negotiate
// features (e.g. per-agent credentials). The hub still accepts a frame without
// "v" from older agents, so this is advisory, not a handshake gate.
const protocolVersion = 1

type msg map[string]any

// encodeMessage frames a message as a 4-byte big-endian length prefix + JSON,
// matching the hub's struct.pack("!I", len) framing.
func encodeMessage(m msg) ([]byte, error) {
	payload, err := json.Marshal(m)
	if err != nil {
		return nil, err
	}
	if len(payload) > maxMessageSize {
		return nil, fmt.Errorf("%w: %d bytes (max %d)", errFrameTooLarge, len(payload), maxMessageSize)
	}
	buf := make([]byte, 4+len(payload))
	binary.BigEndian.PutUint32(buf[:4], uint32(len(payload)))
	copy(buf[4:], payload)
	return buf, nil
}

// readMessage reads one length-prefixed JSON message.
func readMessage(r io.Reader) (msg, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > maxMessageSize {
		return nil, fmt.Errorf("message too large: %d bytes (max %d)", n, maxMessageSize)
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	var m msg
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, err
	}
	return m, nil
}

func str(m msg, key string) string {
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}
