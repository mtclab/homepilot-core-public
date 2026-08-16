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
	"strings"
	"sync"
	"time"
)

type Agent struct {
	cfg     Config
	exec    Executor
	agentID string

	conn    net.Conn
	writeMu sync.Mutex

	// usingFileToken is true while cfg.AuthToken is the durable token loaded from
	// (or persisted to) HP_AGENT_TOKEN_FILE, rather than the env enrollment
	// token. It drives the self-heal in register(): a stored credential the hub
	// rejects must not be retried forever.
	usingFileToken bool

	// Replay-protection state (#362 slice 3). hasPerAgent is durable across the
	// process (set once a per-agent token is held/adopted); the rest are
	// per-connection and reset in register(). When replayOn is set, every frame
	// in both directions carries a monotonic seq + HMAC keyed by replayKey.
	hasPerAgent  bool
	replayOn     bool
	replayKey    []byte
	sendSeq      uint64
	recvExpected uint64
}

func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func (a *Agent) send(m msg) error {
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	// On a replay-protected connection, stamp a monotonic seq + MAC under the
	// write lock so the on-wire order matches the assigned sequence.
	if a.replayOn {
		a.sendSeq++
		m["seq"] = a.sendSeq
		mac, err := computeMAC(a.replayKey, m)
		if err != nil {
			return err
		}
		m["mac"] = mac
	}
	b, err := encodeMessage(m)
	if err != nil {
		return err
	}
	_, err = a.conn.Write(b)
	return err
}

// verifyInbound checks a frame's seq + MAC on a replay-protected connection and
// advances the expected sequence on success. Any failure is returned so the
// caller closes the connection (fail-closed).
func (a *Agent) verifyInbound(m msg) error {
	seqf, ok := m["seq"].(float64)
	if !ok || uint64(seqf) != a.recvExpected {
		return fmt.Errorf("replay: bad seq (expected %d, got %v)", a.recvExpected, m["seq"])
	}
	mac, ok := m["mac"].(string)
	if !ok || !verifyMAC(a.replayKey, m, mac) {
		return fmt.Errorf("replay: bad mac")
	}
	a.recvExpected++
	return nil
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
	// This connection's register frame is never seq/MAC framed (freshness is
	// carried by nonce+ts instead); framing starts only after a successful ack.
	a.replayOn = false

	m := msg{
		"action":      "register",
		"agent_id":    a.agentID,
		"hostname":    hostnameOrUnknown(),
		"system_info": collectSystemInfo(),
		"auth_token":  a.cfg.AuthToken,
		"request_id":  newUUID(),
		"v":           protocolVersion,
	}
	// Negotiate replay protection when we hold a per-agent token (a durable
	// shared secret usable as the MAC key). The register frame gains a fresh
	// nonce + timestamp so it cannot itself be replayed.
	negotiateReplay := a.hasPerAgent
	if negotiateReplay {
		nonce, err := newNonceHex()
		if err != nil {
			return err
		}
		m["replay"] = 1
		m["nonce"] = nonce
		m["ts"] = time.Now().Unix()
		a.replayKey = []byte(a.cfg.AuthToken)
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
		if isAuthError(e) {
			a.rotateTokenSource()
		}
		return fmt.Errorf("registration failed: %s", e)
	}
	if id := str(resp, "agent_id"); id != "" {
		a.agentID = id
	}
	// Durable-credential handback: if the hub returns a durable token (we may
	// have enrolled with a one-time bootstrap token), adopt + persist it so we
	// reconnect across hub restarts without manual re-enrollment. Holding a
	// per-agent token means the NEXT connection can negotiate replay protection.
	if dt := str(resp, "auth_token"); dt != "" && dt != a.cfg.AuthToken {
		a.cfg.AuthToken = dt
		a.hasPerAgent = true
		// The live credential is now the durable per-agent one; a later rejection
		// of it may fall back to the env enrollment token again.
		a.usingFileToken = true
		if a.cfg.AuthTokenFile != "" {
			if err := os.WriteFile(a.cfg.AuthTokenFile, []byte(dt), 0o600); err != nil {
				log.Printf("warning: could not persist durable token to %s: %v", a.cfg.AuthTokenFile, err)
			} else {
				log.Printf("adopted durable hub token (persisted to %s)", a.cfg.AuthTokenFile)
			}
		}
	}
	// Enable per-frame framing for this connection only when we asked for it AND
	// the hub advertises the replay-v1 feature (so an older hub stays compatible).
	if negotiateReplay && featuresContain(resp, "replay-v1") {
		a.sendSeq = 0
		a.recvExpected = 1
		a.replayOn = true
		log.Printf("replay protection enabled for this connection")
	}
	log.Printf("registered as agent %s", a.agentID)
	return nil
}

// isAuthError reports whether a hub error reply means "your credential was not
// accepted" (as opposed to e.g. a duplicate-identity or freshness rejection,
// which changing credentials would not fix).
func isAuthError(e string) bool {
	return strings.Contains(strings.ToLower(e), "auth")
}

// rotateTokenSource self-heals a rejected credential by ALTERNATING between the
// two credentials the agent may hold: the durable per-agent token persisted in
// HP_AGENT_TOKEN_FILE and the configured enrollment token (HP_AGENT_AUTH_TOKEN,
// shared or bootstrap).
//
// Without this, an agent whose stored per-agent token the hub can no longer
// match (a rebuilt hub database, a revoked-then-reissued credential, an id that
// drifted) retries the same dead token forever and is banned after the hub's
// consecutive-auth-failure limit. Alternating (never hammering one source)
// guarantees the agent can always re-enroll; the caller keeps its existing
// exponential backoff, so this adds no extra connection pressure.
func (a *Agent) rotateTokenSource() {
	if a.usingFileToken {
		env := a.cfg.EnvAuthToken
		if env == "" || env == a.cfg.AuthToken {
			return // nothing else to try
		}
		a.cfg.AuthToken = env
		a.usingFileToken = false
		// A shared/bootstrap token is not a per-agent MAC key: don't negotiate
		// replay protection with it (the hub would refuse anyway).
		a.hasPerAgent = false
		log.Printf("hub rejected the stored per-agent credential; retrying with the configured enrollment token")
		return
	}
	stored := resolveToken(a.cfg.AuthTokenFile, "")
	if stored == "" || stored == a.cfg.AuthToken {
		return // nothing else to try
	}
	a.cfg.AuthToken = stored
	a.usingFileToken = true
	a.hasPerAgent = true
	log.Printf("hub rejected the enrollment token; retrying with the stored per-agent credential")
}

// featuresContain reports whether a register_ack advertises the named feature.
func featuresContain(resp msg, want string) bool {
	feats, ok := resp["features"].([]any)
	if !ok {
		return false
	}
	for _, f := range feats {
		if s, ok := f.(string); ok && s == want {
			return true
		}
	}
	return false
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
		// Fail-closed replay check on every inbound frame (heartbeat_ack and other
		// acks included) before acting on it.
		if a.replayOn {
			if err := a.verifyInbound(m); err != nil {
				return err
			}
		}
		reqID := str(m, "request_id")
		switch str(m, "action") {
		case "exec":
			a.handleExec(m, reqID)
		case "read_file":
			a.handleReadFile(m, reqID)
		case "write_file":
			a.handleWriteFile(m, reqID)
		case "install_package":
			a.handleInstallPackage(m, reqID)
		case "manage_service":
			a.handleManageService(m, reqID)
		case "write_config":
			a.handleWriteConfig(m, reqID)
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

// handleInstallPackage / handleManageService / handleWriteConfig dispatch the
// #397 phase-B1 provisioning actions. Each parses its args, runs the idempotent
// action through the agent's own allowlist-checked exec / write path, and
// returns a structured {changed, detail} result (or an error frame). The
// existing send() path stamps replay framing.
func (a *Agent) handleInstallPackage(m msg, reqID string) {
	res, err := installPackage(a.exec.Exec, a.exec.allow.privileged, str(m, "name"))
	a.sendProvisionResult(res, err, reqID)
}

func (a *Agent) handleManageService(m msg, reqID string) {
	res, err := manageService(a.exec.Exec, a.exec.allow.privileged, str(m, "name"), str(m, "state"))
	a.sendProvisionResult(res, err, reqID)
}

func (a *Agent) handleWriteConfig(m msg, reqID string) {
	mode := str(m, "mode")
	if mode == "" {
		mode = "0644"
	}
	res, err := writeConfig(a.exec.allow.privileged, str(m, "path"), str(m, "content"), mode)
	a.sendProvisionResult(res, err, reqID)
}

// sendProvisionResult frames a provisioning outcome as a command_result: an
// error frame on failure, else a structured {changed, detail} success.
func (a *Agent) sendProvisionResult(res provisionResult, err error, reqID string) {
	if err != nil {
		_ = a.send(msg{"action": "command_result", "error": err.Error(), "request_id": reqID})
		return
	}
	_ = a.send(msg{
		"action": "command_result", "status": "ok",
		"changed": res.Changed, "detail": res.Detail, "request_id": reqID,
	})
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
		cfg:  cfg,
		exec: Executor{allow: Allowlist{privileged: cfg.Privileged}},
		// A STABLE id across restarts: HP_AGENT_ID, else the persisted id file,
		// else a freshly generated id that is written to that file.
		agentID:        resolveAgentID(cfg.AgentID, cfg.AgentIDFile),
		hasPerAgent:    cfg.HasPersistedToken,
		usingFileToken: cfg.HasPersistedToken,
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
