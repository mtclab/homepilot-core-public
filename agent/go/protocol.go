package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
)

const maxMessageSize = 1 << 20 // 1 MiB, matches the hub

type msg map[string]any

// encodeMessage frames a message as a 4-byte big-endian length prefix + JSON,
// matching the hub's struct.pack("!I", len) framing.
func encodeMessage(m msg) ([]byte, error) {
	payload, err := json.Marshal(m)
	if err != nil {
		return nil, err
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
