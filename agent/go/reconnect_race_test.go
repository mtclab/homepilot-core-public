package main

import (
	"io"
	"net"
	"strconv"
	"sync"
	"testing"
)

// Reconnection must be free of data races on the live socket.
//
// `a.conn` is written by connect() under writeMu and read by every sender under
// the same lock - but closeConn() read it BARE. closeConn is called from the
// reconnect backoff loop and from handleSetTransport (the hub-pushed TLS
// migration), both concurrent with connect() publishing the next socket, so the
// read could observe a torn interface value: the wrong socket closed, or a
// dereference of half a value.
//
// This test drives connect / closeConn / sendOn concurrently against a real
// listener. It is written for `go test -race` (make gate-go-race); without the
// race detector it only proves the paths do not deadlock or panic.
//
// Teeth: revert closeConn to `if a.conn != nil { _ = a.conn.Close() }` and
// `go test -race -run TestReconnectHasNoRaceOnTheLiveSocket ./...` fails with
// "WARNING: DATA RACE ... Read at ... closeConn ... Previous write at ...
// connect".
func TestReconnectHasNoRaceOnTheLiveSocket(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = io.Copy(io.Discard, c)
			}(c)
		}
	}()

	host, portStr, err := net.SplitHostPort(ln.Addr().String())
	if err != nil {
		t.Fatalf("split addr: %v", err)
	}
	port, err := strconv.Atoi(portStr)
	if err != nil {
		t.Fatalf("port: %v", err)
	}

	a := &Agent{cfg: Config{HubHost: host, HubPort: port, HeartbeatInterval: 1}}
	if err := a.connect(); err != nil {
		t.Fatalf("initial connect: %v", err)
	}

	const rounds = 150
	var wg sync.WaitGroup

	// The reconnect loop: close the old socket, dial the next one.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < rounds; i++ {
			a.closeConn()
			_ = a.connect()
		}
	}()

	// A second closer, standing in for handleSetTransport dropping the socket
	// while the backoff loop is also reconnecting.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < rounds; i++ {
			a.closeConn()
		}
	}()

	// Heartbeats, which is what actually writes on the socket concurrently.
	for k := 0; k < 4; k++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				gen := a.currentGen()
				_ = a.sendOn(gen, msg{"action": "heartbeat", "request_id": newUUID()})
			}
		}()
	}

	wg.Wait()
	a.closeConn()
}
