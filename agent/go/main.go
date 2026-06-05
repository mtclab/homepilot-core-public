// hp-agent — HomePilot managed-host agent (Go port of hp_agent).
//
// Connects outbound to the HomePilot agent hub over a persistent TCP connection
// (length-prefixed JSON framing), registers, and serves exec / file-ops /
// zabbix-push requests with command + path allowlists. Pure stdlib; builds as a
// static binary (CGO_ENABLED=0) for amd64 + arm64.
package main

import (
	"crypto/rand"
	"crypto/tls"
	"fmt"
	"log"
	"net"
	"os"
	"sync"
	"time"
)

type Agent struct {
	cfg     Config
	exec    Executor
	agentID string

	conn    net.Conn
	writeMu sync.Mutex
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func (a *Agent) send(m msg) error {
	b, err := encodeMessage(m)
	if err != nil {
		return err
	}
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	_, err = a.conn.Write(b)
	return err
}

func (a *Agent) connect() error {
	addr := fmt.Sprintf("%s:%d", a.cfg.HubHost, a.cfg.HubPort)
	tlsCfg, err := a.cfg.tlsConfig()
	if err != nil {
		return err
	}
	label := ""
	var conn net.Conn
	if tlsCfg != nil {
		label = " (TLS)"
		conn, err = tls.DialWithDialer(&net.Dialer{Timeout: 10 * time.Second}, "tcp", addr, tlsCfg)
	} else {
		conn, err = net.DialTimeout("tcp", addr, 10*time.Second)
	}
	if err != nil {
		return err
	}
	a.conn = conn
	log.Printf("connecting to hub at %s%s", addr, label)
	return nil
}

func (a *Agent) register() error {
	m := msg{
		"action":      "register",
		"agent_id":    a.agentID,
		"hostname":    hostnameOrUnknown(),
		"system_info": collectSystemInfo(),
		"auth_token":  a.cfg.AuthToken,
		"request_id":  newUUID(),
	}
	if err := a.send(m); err != nil {
		return err
	}
	_ = a.conn.SetReadDeadline(time.Now().Add(30 * time.Second))
	resp, err := readMessage(a.conn)
	if err != nil {
		return err
	}
	if e := str(resp, "error"); e != "" {
		return fmt.Errorf("registration failed: %s", e)
	}
	if id := str(resp, "agent_id"); id != "" {
		a.agentID = id
	}
	log.Printf("registered as agent %s", a.agentID)
	return nil
}

func hostnameOrUnknown() string {
	if h, err := os.Hostname(); err == nil {
		return h
	}
	return "unknown"
}

// connectWithRetry connects + registers with capped exponential backoff.
func (a *Agent) connectWithRetry() {
	delay := time.Second
	for attempt := 1; ; attempt++ {
		if err := a.connect(); err == nil {
			if err = a.register(); err == nil {
				if attempt > 1 {
					log.Printf("connected to hub after %d attempts", attempt)
				}
				return
			} else {
				log.Printf("connect attempt %d failed (%v); retrying in %.0fs", attempt, err, delay.Seconds())
			}
		} else {
			log.Printf("connect attempt %d failed (%v); retrying in %.0fs", attempt, err, delay.Seconds())
		}
		a.closeConn()
		time.Sleep(delay)
		if delay < 30*time.Second {
			delay *= 2
			if delay > 30*time.Second {
				delay = 30 * time.Second
			}
		}
	}
}

func (a *Agent) closeConn() {
	if a.conn != nil {
		_ = a.conn.Close()
	}
}

func (a *Agent) heartbeatLoop(done <-chan struct{}) {
	t := time.NewTicker(time.Duration(a.cfg.HeartbeatInterval) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			if err := a.send(msg{"action": "heartbeat", "request_id": newUUID()}); err != nil {
				return
			}
		}
	}
}

func (a *Agent) zabbixLoop(done <-chan struct{}) {
	if !a.cfg.ZabbixEnabled {
		return
	}
	host := a.cfg.ZabbixHostname
	if host == "" {
		host = hostnameOrUnknown()
	}
	t := time.NewTicker(time.Duration(a.cfg.ZabbixInterval) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			items := systemInfoToMetrics(host, collectSystemInfo())
			if _, ok := zabbixSend(a.cfg.ZabbixServer, a.cfg.ZabbixPort, items); !ok {
				log.Printf("zabbix metrics send failed")
			}
		}
	}
}

// serve runs the read/dispatch loop for one connection until it errors.
func (a *Agent) serve() error {
	done := make(chan struct{})
	defer close(done)
	go a.heartbeatLoop(done)
	go a.zabbixLoop(done)

	for {
		_ = a.conn.SetReadDeadline(time.Now().Add(300 * time.Second))
		m, err := readMessage(a.conn)
		if err != nil {
			return err
		}
		reqID := str(m, "request_id")
		switch str(m, "action") {
		case "exec":
			a.handleExec(m, reqID)
		case "read_file":
			a.handleReadFile(m, reqID)
		case "write_file":
			a.handleWriteFile(m, reqID)
		case "zabbix_push":
			a.handleZabbixPush(m, reqID)
		default:
			// Acks (heartbeat_ack/result_ack/register_ack) and anything else:
			// drain + ignore (don't ping-pong the hub with errors).
		}
	}
}

func (a *Agent) handleExec(m msg, reqID string) {
	command := str(m, "command")
	timeout := 30
	if t, ok := m["timeout"].(float64); ok {
		timeout = int(t)
	}
	code, stdout, stderr := a.exec.Exec(command, timeout)
	_ = a.send(msg{
		"action": "command_result", "exit_code": code,
		"stdout": stdout, "stderr": stderr, "request_id": reqID,
	})
}

func (a *Agent) handleReadFile(m msg, reqID string) {
	content, err := readFile(str(m, "path"))
	if err != nil {
		_ = a.send(msg{"action": "command_result", "error": err.Error(), "request_id": reqID})
		return
	}
	_ = a.send(msg{"action": "command_result", "content": content, "request_id": reqID})
}

func (a *Agent) handleWriteFile(m msg, reqID string) {
	if err := writeFile(str(m, "path"), str(m, "content")); err != nil {
		_ = a.send(msg{"action": "command_result", "error": err.Error(), "request_id": reqID})
		return
	}
	_ = a.send(msg{"action": "command_result", "status": "ok", "request_id": reqID})
}

func (a *Agent) handleZabbixPush(m msg, reqID string) {
	if !a.cfg.ZabbixEnabled {
		_ = a.send(msg{"error": "zabbix not configured", "request_id": reqID})
		return
	}
	host := a.cfg.ZabbixHostname
	if host == "" {
		host = hostnameOrUnknown()
	}
	items := systemInfoToMetrics(host, collectSystemInfo())
	sent, ok := zabbixSend(a.cfg.ZabbixServer, a.cfg.ZabbixPort, items)
	_ = a.send(msg{
		"action": "zabbix_push_result", "metrics_sent": sent,
		"zabbix_ok": ok, "request_id": reqID,
	})
}

func (a *Agent) run() {
	for {
		a.connectWithRetry()
		err := a.serve()
		log.Printf("hub connection lost (%v), reconnecting...", err)
		a.closeConn()
	}
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.SetPrefix("[hp-agent] ")
	cfg := ConfigFromEnv()
	agent := &Agent{
		cfg:     cfg,
		exec:    Executor{allow: Allowlist{privileged: cfg.Privileged}},
		agentID: cfg.AgentID,
	}
	if agent.agentID == "" {
		agent.agentID = newUUID()
	}
	if cfg.ZabbixEnabled {
		host := cfg.ZabbixHostname
		if host == "" {
			host = hostnameOrUnknown()
		}
		log.Printf("Zabbix trapper enabled: %s:%d as %s", cfg.ZabbixServer, cfg.ZabbixPort, host)
	}
	agent.run()
}
