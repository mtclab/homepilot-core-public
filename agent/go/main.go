// hp-agent — HomePilot managed-host agent (Go port of hp_agent).
//
// Connects outbound to the HomePilot agent hub over a persistent TCP connection
// (length-prefixed JSON framing), registers, serves exec / file-ops requests
// with command + path allowlists, and reports system metrics on an interval.
// Pure stdlib; builds as a static binary (CGO_ENABLED=0) for amd64 + arm64.
package main

import (
	"crypto/rand"
	"crypto/tls"
	"errors"
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
	// connGen identifies the LIVE connection. Per-connection loops capture it and
	// send through sendOn, so a loop that has not noticed the drop yet cannot
	// write onto the socket that replaced it. Read and written under writeMu.
	connGen uint64

	// Bounded FIFO of metric samples awaiting delivery. Lives on the Agent, not
	// on a connection, so a hub restart delays the series instead of holing it.
	metrics *metricBuffer

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

// errStaleConn is returned by sendOn when the caller's connection has already
// been replaced. It is an expected outcome during a reconnect, not a fault.
var errStaleConn = fmt.Errorf("connection replaced")

func (a *Agent) send(m msg) error {
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	return a.sendLocked(m)
}

// sendOn writes m only while gen is still the live connection.
//
// Without this a loop belonging to a dropped connection could stamp the NEXT
// connection's sequence number onto a stale frame: the hub verifies sequences
// fail-closed, so the frame would look like a replay and cost the agent the
// connection it had just established.
func (a *Agent) sendOn(gen uint64, m msg) error {
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	if a.connGen != gen {
		return errStaleConn
	}
	return a.sendLocked(m)
}

// sendLocked is the write path; the caller must hold writeMu.
func (a *Agent) sendLocked(m msg) error {
	// On a replay-protected connection, stamp a monotonic seq + MAC under the
	// write lock so the on-wire order matches the assigned sequence.
	//
	// A frame that fails to ENCODE must give its sequence number back. The hub
	// verifies sequences fail-closed, so a consumed-but-unsent seq would make
	// every subsequent frame look like a replay and cost the agent the
	// connection - turning "this answer is too big" into "this host is gone".
	if a.replayOn {
		a.sendSeq++
		m["seq"] = a.sendSeq
		mac, err := computeMAC(a.replayKey, m)
		if err != nil {
			a.sendSeq--
			return err
		}
		m["mac"] = mac
	}
	b, err := encodeMessage(m)
	if err != nil {
		if a.replayOn {
			a.sendSeq--
		}
		return err
	}
	_, err = a.conn.Write(b)
	return err
}

// sendResult writes a command_result, degrading to a compact error frame when
// the result itself will not fit on the wire.
//
// The per-payload budgets (maxPayloadBytes, execStreamBudget) are what normally
// keep a reply sendable; this is the backstop behind them. It matters because
// an unsendable frame is not a local error: the hub refuses to parse a body it
// cannot MAC-verify, so an oversize reply CLOSES the connection, and the caller
// that asked the question gets neither an answer nor a reason.
func (a *Agent) sendResult(m msg, forAction, reqID string) {
	err := a.send(m)
	if err == nil || !errors.Is(err, errFrameTooLarge) {
		return
	}
	log.Printf("refusing to send an oversize %s result for %s: %v", forAction, reqID, err)
	_ = a.send(msg{
		"action": "command_result", "for_action": forAction, "request_id": reqID,
		"error": fmt.Sprintf(
			"the %s result is too large to return over the agent hub protocol (%v)",
			forAction, err),
	})
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
	// Publish the socket and its generation together under the write lock, so a
	// sender from the previous connection can never observe the new socket with
	// the old generation.
	a.writeMu.Lock()
	a.conn = conn
	a.connGen++
	a.writeMu.Unlock()
	log.Printf("connecting to hub at %s%s", addr, label)
	return nil
}

// currentGen reports the live connection generation.
func (a *Agent) currentGen() uint64 {
	a.writeMu.Lock()
	defer a.writeMu.Unlock()
	return a.connGen
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
		// Declare that every command_result this agent sends echoes the issued
		// action back as "for_action". The hub holds a declaring agent to it and
		// drops a reply that answers a different request than the one it claims
		// (#381 confused deputy); an agent that does not declare it is exempt, so
		// a hub upgraded ahead of its fleet still works.
		"result_action": 1,
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
			// Atomic (temp + fsync + rename): a truncated token file is a
			// permanent lockout, not a retryable error.
			if err := writeAgentTokenFile(a.cfg.AuthTokenFile, dt); err != nil {
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

// closeConn drops the live socket.
//
// It reads a.conn under writeMu, like every other reader and writer of that
// field. It used to read it bare, which is a data race against connect()
// publishing the next socket: closeConn is called from the reconnect loop and
// from handleSetTransport (the hub-pushed TLS migration), so "close the old one"
// could observe a torn interface value and close - or dereference - the wrong
// thing. The generation-scoped writes (sendOn) fixed the write path; this read
// was missed.
func (a *Agent) closeConn() {
	a.writeMu.Lock()
	conn := a.conn
	a.writeMu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}

func (a *Agent) heartbeatLoop(done <-chan struct{}, gen uint64) {
	t := time.NewTicker(time.Duration(a.cfg.HeartbeatInterval) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			if err := a.sendOn(gen, msg{"action": "heartbeat", "request_id": newUUID()}); err != nil {
				return
			}
		}
	}
}

// serve runs the read/dispatch loop for one connection until it errors.
func (a *Agent) serve() error {
	done := make(chan struct{})
	defer close(done)
	// Metrics acks are routed from this read loop to the metrics loop, which
	// drops a batch from the buffer only once the hub has confirmed it.
	acks := make(chan string, 4)
	gen := a.currentGen()
	go a.heartbeatLoop(done, gen)
	go a.metricsLoop(done, acks, gen)

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
		case "set_transport":
			a.handleSetTransport(m, reqID)
		case "metrics_ack":
			// Non-blocking: a late/duplicate ack must never stall the read loop.
			select {
			case acks <- reqID:
			default:
			}
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
	a.sendResult(msg{
		"action": "command_result", "for_action": "exec", "exit_code": code,
		"stdout": stdout, "stderr": stderr, "request_id": reqID,
	}, "exec", reqID)
}

func (a *Agent) handleReadFile(m msg, reqID string) {
	content, err := readFile(str(m, "path"))
	if err != nil {
		_ = a.send(msg{"action": "command_result", "for_action": "read_file",
			"error": err.Error(), "request_id": reqID})
		return
	}
	a.sendResult(msg{"action": "command_result", "for_action": "read_file",
		"content": content, "request_id": reqID}, "read_file", reqID)
}

func (a *Agent) handleWriteFile(m msg, reqID string) {
	if err := writeFile(str(m, "path"), str(m, "content")); err != nil {
		_ = a.send(msg{"action": "command_result", "for_action": "write_file",
			"error": err.Error(), "request_id": reqID})
		return
	}
	_ = a.send(msg{"action": "command_result", "for_action": "write_file",
		"status": "ok", "request_id": reqID})
}

// handleInstallPackage / handleManageService / handleWriteConfig dispatch the
// #397 phase-B1 provisioning actions. Each parses its args, runs the idempotent
// action through the agent's own allowlist-checked exec / write path, and
// returns a structured {changed, detail} result (or an error frame). The
// existing send() path stamps replay framing.
func (a *Agent) handleInstallPackage(m msg, reqID string) {
	res, err := installPackage(a.exec.Exec, a.exec.allow.privileged,
		a.exec.allow.allowPackageInstall, str(m, "name"))
	a.sendProvisionResult("install_package", res, err, reqID)
}

func (a *Agent) handleManageService(m msg, reqID string) {
	res, err := manageService(a.exec.Exec, a.exec.allow.privileged, str(m, "name"), str(m, "state"))
	a.sendProvisionResult("manage_service", res, err, reqID)
}

func (a *Agent) handleWriteConfig(m msg, reqID string) {
	mode := str(m, "mode")
	if mode == "" {
		mode = "0644"
	}
	res, err := writeConfig(a.exec.allow.privileged, str(m, "path"), str(m, "content"), mode)
	a.sendProvisionResult("write_config", res, err, reqID)
}

// sendProvisionResult frames a provisioning outcome as a command_result: an
// error frame on failure, else a structured {changed, detail} success.
func (a *Agent) sendProvisionResult(forAction string, res provisionResult, err error, reqID string) {
	if err != nil {
		_ = a.send(msg{"action": "command_result", "for_action": forAction,
			"error": err.Error(), "request_id": reqID})
		return
	}
	_ = a.send(msg{
		"action": "command_result", "for_action": forAction, "status": "ok",
		"changed": res.Changed, "detail": res.Detail, "request_id": reqID,
	})
}

// handleSetTransport applies a transport the hub pushed - today, "move to TLS
// and here is my certificate's fingerprint" (#468).
//
// This exists because the alternative is editing /etc/homepilot/agent.env on
// every managed host: the TLS env vars are written once at enrolment and
// nothing rewrites them, so a hub that turned on TLS stranded its whole fleet
// with no way back through the channel that just closed.
//
// Order matters. The pin is PARSED BEFORE ANYTHING IS PERSISTED OR ACKED,
// because a stored pin that cannot be parsed is unrecoverable in exactly the
// way this feature is meant to prevent: the agent would refuse every handshake
// with the hub it needs in order to be fixed. A rejected push leaves the agent
// on its current, working transport and tells the hub why.
func (a *Agent) handleSetTransport(m msg, reqID string) {
	wantTLS, _ := m["tls"].(bool)
	pin := str(m, "pin")

	if !wantTLS {
		// Only the plaintext -> TLS direction is supported. Accepting the reverse
		// would let anyone who reached the channel talk a fleet back down onto
		// plaintext, which is a downgrade attack with extra steps.
		_ = a.send(msg{
			"action": "command_result", "for_action": "set_transport", "request_id": reqID,
			"error": "set_transport: only enabling TLS is supported",
		})
		return
	}
	if _, err := parsePin(pin); err != nil {
		_ = a.send(msg{
			"action": "command_result", "for_action": "set_transport", "request_id": reqID,
			"error": fmt.Sprintf("set_transport: unusable pin, keeping current transport: %v", err),
		})
		return
	}

	if err := writeTransportFile(a.cfg.TransportFile, persistedTransport{TLS: true, Pin: pin}); err != nil {
		_ = a.send(msg{
			"action": "command_result", "for_action": "set_transport", "request_id": reqID,
			"error": fmt.Sprintf("set_transport: could not persist to %s: %v", a.cfg.TransportFile, err),
		})
		return
	}

	// Ack on the CURRENT connection before dropping it: the hub counts acks to
	// decide when the fleet is ready for the listener to flip, and an ack sent
	// after the reconnect would arrive on a socket the hub is not waiting on.
	_ = a.send(msg{
		"action": "command_result", "for_action": "set_transport", "status": "ok", "request_id": reqID,
		"transport": "tls", "persisted_to": a.cfg.TransportFile,
	})

	a.cfg.TLS = true
	if a.cfg.TLSPin == "" {
		a.cfg.TLSPin = pin
	}
	log.Printf("hub pushed a TLS transport (persisted to %s); reconnecting", a.cfg.TransportFile)
	// Dropping the socket returns serve(), and run() redials - now over TLS.
	// Nothing here restarts the process, so a pushed transport survives without
	// systemd's help and applies immediately.
	a.closeConn()
}

func (a *Agent) run() {
	// One sampler for the process: metrics keep being collected across
	// reconnects, which is what makes the buffer worth having.
	go a.samplerLoop(nil)
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
	// Answer "which binary is this?" without a hub, a token or a network: after
	// the 2.6.0 regression the only way to find the hosts still running the
	// broken binary was to SSH each one (#430).
	for _, arg := range os.Args[1:] {
		if arg == "--version" || arg == "-version" || arg == "-v" {
			fmt.Printf("hp-agent %s\n", agentVersion())
			return
		}
	}
	cfg := ConfigFromEnv()
	// Fail closed, at startup, loudly: an agent that was TOLD to be privileged but
	// cannot do privileged work must say so now, not fail opaquely on the first
	// hub request hours later (#422).
	report, err := preflight(cfg, os.Geteuid(), writeAllowedPrefixes())
	for _, line := range report {
		log.Print(line)
	}
	if err != nil {
		log.Printf("FATAL: %v", err)
		os.Exit(1)
	}
	agent := &Agent{
		cfg: cfg,
		exec: Executor{allow: Allowlist{
			privileged:          cfg.Privileged,
			allowPackageInstall: cfg.AllowPackageInstall,
		}},
		// A STABLE id across restarts: HP_AGENT_ID, else the persisted id file,
		// else a freshly generated id that is written to that file.
		agentID:        resolveAgentID(cfg.AgentID, cfg.AgentIDFile),
		hasPerAgent:    cfg.HasPersistedToken,
		usingFileToken: cfg.HasPersistedToken,
		metrics:        newMetricBuffer(cfg.MetricsBuffer),
	}
	if cfg.MetricsEnabled {
		log.Printf("metrics enabled: every %ds, buffering up to %d samples while offline",
			cfg.MetricsInterval, cfg.MetricsBuffer)
	} else {
		log.Printf("metrics DISABLED (HP_AGENT_METRICS_ENABLED=false) - this host reports no metrics")
	}
	agent.run()
}
