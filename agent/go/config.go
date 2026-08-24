package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// defaultAgentIDFile is where the generated agent id is persisted when neither
// HP_AGENT_ID_FILE nor HP_AGENT_TOKEN_FILE gives a location to derive one from.
const defaultAgentIDFile = "/etc/homepilot/agent.id"

// defaultTransportFile is where a hub-pushed transport migration is persisted
// when neither HP_AGENT_TRANSPORT_FILE nor HP_AGENT_TOKEN_FILE locates one.
const defaultTransportFile = "/etc/homepilot/agent.transport"

// Config mirrors hp_agent.config.AgentConfig (env-driven).
type Config struct {
	HubHost       string
	HubPort       int
	AuthToken     string
	AuthTokenFile string
	// EnvAuthToken is the raw HP_AGENT_AUTH_TOKEN (the shared/bootstrap
	// enrollment credential). It is kept alongside the resolved AuthToken so the
	// agent can fall back to it and re-enroll when the persisted per-agent token
	// is rejected by the hub, instead of retrying a dead credential forever.
	EnvAuthToken string
	AgentID      string
	// AgentIDFile is where a generated agent id is persisted so it stays STABLE
	// across restarts. An unstable id breaks reconnection under per-agent
	// credentials: the persisted token is bound to the id it was issued to.
	AgentIDFile       string
	HeartbeatInterval int
	LogLevel          string
	TLS               bool
	TLSCa             string
	TLSCert           string
	TLSKey            string
	// TLSPin is the sha256 (hex, optional "sha256:" prefix) of the hub
	// certificate's DER encoding, handed to the installer at enrollment. The hub
	// generates its own self-signed certificate, which chains to nothing in the
	// system trust store, so the pin is what makes "verify the hub" mean
	// something. Set => the exact certificate is required.
	TLSPin string
	Privileged        bool
	// AllowPackageInstall is the second-stage grant for apt/apt-get. Installing a
	// package writes across the whole filesystem, so the systemd unit has to drop
	// its ProtectSystem= boundary for it; that is a separate operator decision
	// from "may manage services and config files" (#422).
	AllowPackageInstall bool
	// Native metrics (ADR-004 S5). On by default: monitoring is part of the
	// product, not an operator decision. MetricsBuffer bounds how many samples
	// are held while the hub is unreachable; past it the OLDEST are dropped and
	// the drop is logged (see metricBuffer).
	MetricsEnabled  bool
	MetricsInterval int
	MetricsBuffer   int
	// HasPersistedToken is true when a durable per-agent token was loaded from
	// HP_AGENT_TOKEN_FILE. Such a token is a shared secret usable as the replay
	// MAC key, so the agent negotiates replay protection (#362 slice 3) on
	// connect. An agent enrolling for the first time (env/bootstrap token only)
	// has no per-agent key yet and does not negotiate.
	HasPersistedToken bool
	// TransportFile is where a hub-pushed transport migration is persisted, so
	// "move this fleet to TLS" never means editing /etc/homepilot/agent.env on
	// every managed host (#468). The env vars are written once at enrolment and
	// nothing rewrites them, which is why an upgrade that flipped the hub to TLS
	// stranded every already-enrolled agent - a newer binary alone kept dialling
	// plaintext. A persisted transport OVERRIDES the env, for the same reason the
	// persisted token does: it is the newer instruction, delivered over the
	// authenticated channel by the hub this agent belongs to.
	TransportFile string
}

// transportFilePath decides where a hub-pushed transport is persisted:
// HP_AGENT_TRANSPORT_FILE when set, else "agent.transport" beside the token,
// else the packaged default - the same derivation the agent id uses, so id,
// credential and transport share one directory and one set of permissions.
func transportFilePath(transportFile, tokenFile string) string {
	if transportFile != "" {
		return transportFile
	}
	if tokenFile != "" {
		return filepath.Join(filepath.Dir(tokenFile), "agent.transport")
	}
	return defaultTransportFile
}

// persistedTransport is what the hub pushed, as stored on disk.
type persistedTransport struct {
	TLS bool   `json:"tls"`
	Pin string `json:"pin"`
}

// readTransportFile returns the persisted transport, or ok=false when there is
// none to apply. A malformed file is treated as absent rather than fatal: the
// agent must still come up on its configured transport and let the hub push
// again, instead of refusing to start over a file it wrote itself.
func readTransportFile(path string) (persistedTransport, bool) {
	if path == "" {
		return persistedTransport{}, false
	}
	raw, err := os.ReadFile(path) // #nosec G304 -- operator-controlled agent state path
	if err != nil {
		return persistedTransport{}, false
	}
	var stored persistedTransport
	if err := json.Unmarshal(raw, &stored); err != nil {
		log.Printf("warning: ignoring malformed transport file %s: %v", path, err)
		return persistedTransport{}, false
	}
	return stored, true
}

// writeTransportFile persists a hub-pushed transport atomically with 0600, so a
// crash mid-write can never leave the agent with half a pin - which would fail
// every handshake with no way back.
func writeTransportFile(path string, transport persistedTransport) error {
	encoded, err := json.Marshal(transport)
	if err != nil {
		return err
	}
	return writeAgentStateFile(path, string(encoded), ".agent.transport-*")
}

// tokenFileHasContent reports whether path exists and holds a non-empty token.
func tokenFileHasContent(path string) bool {
	if path == "" {
		return false
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return strings.TrimSpace(string(b)) != ""
}

// resolveToken prefers a persisted token file (the durable credential adopted
// after enrollment) over the env token (which may be a one-time bootstrap token
// already consumed by the hub). Falls back to env when the file is missing/empty.
func resolveToken(tokenFile, envToken string) string {
	if tokenFile != "" {
		if b, err := os.ReadFile(tokenFile); err == nil {
			if t := strings.TrimSpace(string(b)); t != "" {
				return t
			}
		}
	}
	return envToken
}

// agentIDFilePath decides where a generated agent id is persisted:
// HP_AGENT_ID_FILE when set, else "agent.id" next to the token file, else the
// packaged default. Keeping it beside the token means the id and the credential
// bound to it share one directory (and one set of file permissions).
func agentIDFilePath(idFile, tokenFile string) string {
	if idFile != "" {
		return idFile
	}
	if tokenFile != "" {
		return filepath.Join(filepath.Dir(tokenFile), "agent.id")
	}
	return defaultAgentIDFile
}

// writeAgentIDFile writes id to path atomically with 0600 permissions, so a
// crash mid-write can never leave a truncated id behind.
func writeAgentIDFile(path, id string) error {
	return writeAgentStateFile(path, id, ".agent.id-*")
}

// writeAgentTokenFile writes the durable per-agent hub credential atomically.
//
// It used to be a plain os.WriteFile, which TRUNCATES before it writes: a crash,
// a power cut, or a full disk between those two steps leaves a zero-length or
// half-written token file. The agent then presents a credential the hub has no
// hash for, is rejected, and — since the shared enrolment token is normally gone
// from the host by then — is locked out permanently, needing a hand re-enrol on
// every affected box. Same failure class as a truncated agent id, same fix.
func writeAgentTokenFile(path, token string) error {
	return writeAgentStateFile(path, token, ".agent.token-*")
}

// writeAgentStateFile writes agent state atomically with 0600 permissions (temp
// file in the same directory + fsync + rename). Shared by the id, the durable
// token and the pushed transport: a half-written pin is as unrecoverable as a
// truncated id or token, since all three are what the agent needs to get back to
// the hub at all.
//
// The fsync matters as much as the rename. A rename is atomic in the directory
// entry, but on a crash the file's DATA can still be missing while the name is
// already published — which is the very truncated-credential outcome this helper
// exists to prevent.
func writeAgentStateFile(path, content, tempPattern string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, tempPattern)
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op once the rename succeeded
	if err := f.Chmod(0o600); err != nil {
		_ = f.Close()
		return err
	}
	if _, err := f.WriteString(content); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// resolveAgentID returns the agent's STABLE identity.
//
// Precedence: an explicit HP_AGENT_ID wins; else a previously persisted id from
// idFile is reused; else a fresh UUID is generated AND persisted so the next
// start reuses it. A brand-new id on every start would orphan the per-agent
// credential minted for the previous id, so the agent would present a token the
// hub cannot match and would be locked out after MAX_AUTH_FAILURES.
//
// If the id cannot be persisted the generated id is still returned (the agent
// runs, loudly warned, rather than refusing to start).
func resolveAgentID(envID, idFile string) string {
	if envID != "" {
		return envID
	}
	if idFile != "" {
		if b, err := os.ReadFile(idFile); err == nil {
			if id := strings.TrimSpace(string(b)); id != "" {
				return id
			}
		}
	}
	id := newUUID()
	if idFile == "" {
		log.Printf("WARNING: no agent-id file configured; using an ephemeral agent id %s "+
			"(set HP_AGENT_ID or HP_AGENT_ID_FILE to make it stable)", id)
		return id
	}
	if err := writeAgentIDFile(idFile, id); err != nil {
		log.Printf("WARNING: could not persist agent id to %s (%v); using the ephemeral id %s "+
			"— this agent will re-enroll on every restart until the path is writable",
			idFile, err, id)
		return id
	}
	log.Printf("generated agent id %s (persisted to %s)", id, idFile)
	return id
}

func envBool(key string) bool {
	switch strings.ToLower(os.Getenv(key)) {
	case "1", "true", "yes":
		return true
	}
	return false
}

// envBoolDefault reads a boolean whose default is not "off". Only an explicit
// falsy value turns such a setting off: an empty variable means "unset", which
// is how an env file spells a setting it does not care about.
func envBoolDefault(key string, def bool) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes":
		return true
	case "0", "false", "no":
		return false
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func ConfigFromEnv() Config {
	cfg := Config{
		HubHost:             envStr("HP_AGENT_HUB_HOST", "localhost"),
		HubPort:             envInt("HP_AGENT_HUB_PORT", 8443),
		AuthToken:           resolveToken(os.Getenv("HP_AGENT_TOKEN_FILE"), os.Getenv("HP_AGENT_AUTH_TOKEN")),
		AuthTokenFile:       os.Getenv("HP_AGENT_TOKEN_FILE"),
		EnvAuthToken:        os.Getenv("HP_AGENT_AUTH_TOKEN"),
		AgentID:             os.Getenv("HP_AGENT_ID"),
		AgentIDFile:         agentIDFilePath(os.Getenv("HP_AGENT_ID_FILE"), os.Getenv("HP_AGENT_TOKEN_FILE")),
		HeartbeatInterval:   envInt("HP_AGENT_HEARTBEAT_INTERVAL", 30),
		LogLevel:            envStr("HP_AGENT_LOG_LEVEL", "INFO"),
		TLS:                 envBool("HP_AGENT_TLS"),
		TLSCa:               os.Getenv("HP_AGENT_TLS_CA"),
		TLSCert:             os.Getenv("HP_AGENT_TLS_CERT"),
		TLSKey:              os.Getenv("HP_AGENT_TLS_KEY"),
		TLSPin:              os.Getenv("HP_AGENT_TLS_PIN"),
		Privileged:          envBool("HP_AGENT_PRIVILEGED"),
		AllowPackageInstall: envBool("HP_AGENT_ALLOW_PACKAGE_INSTALL"),
		MetricsEnabled:      envBoolDefault("HP_AGENT_METRICS_ENABLED", true),
		MetricsInterval:     envInt("HP_AGENT_METRICS_INTERVAL", 60),
		MetricsBuffer:       envInt("HP_AGENT_METRICS_BUFFER", 1440),
		HasPersistedToken:   tokenFileHasContent(os.Getenv("HP_AGENT_TOKEN_FILE")),
		TransportFile: transportFilePath(
			os.Getenv("HP_AGENT_TRANSPORT_FILE"), os.Getenv("HP_AGENT_TOKEN_FILE"),
		),
	}

	// A transport the hub pushed is the NEWER instruction and wins over the env
	// file written once at enrolment - that staleness is exactly what stranded
	// fleets in #468. An explicit HP_AGENT_TLS_PIN is left alone: an operator who
	// pinned a specific certificate by hand has said something the hub may not
	// silently replace.
	if stored, ok := readTransportFile(cfg.TransportFile); ok && stored.TLS {
		cfg.TLS = true
		if cfg.TLSPin == "" {
			cfg.TLSPin = stored.Pin
		}
	}
	return cfg
}

// parsePin normalises a certificate pin to its 32 raw sha256 bytes. Accepts
// "sha256:<hex>", "<hex>", and colon-separated hex. An unparseable pin is an
// error, never a silent fall-through to unpinned TLS.
func parsePin(pin string) ([]byte, error) {
	s := strings.TrimSpace(pin)
	s = strings.TrimPrefix(strings.ToLower(s), "sha256:")
	s = strings.ReplaceAll(s, ":", "")
	raw, err := hex.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("HP_AGENT_TLS_PIN is not hex: %w", err)
	}
	if len(raw) != sha256.Size {
		return nil, fmt.Errorf("HP_AGENT_TLS_PIN must be %d sha256 bytes, got %d", sha256.Size, len(raw))
	}
	return raw, nil
}

// pinVerifier accepts a connection only when the certificate the peer presented
// is byte-for-byte the pinned one.
func pinVerifier(pin []byte) func([][]byte, [][]*x509.Certificate) error {
	return func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
		if len(rawCerts) == 0 {
			return errors.New("hub presented no certificate")
		}
		sum := sha256.Sum256(rawCerts[0])
		if subtle.ConstantTimeCompare(sum[:], pin) != 1 {
			return fmt.Errorf("hub certificate does not match the pinned fingerprint "+
				"(got sha256:%x, expected sha256:%x)", sum[:], pin)
		}
		return nil
	}
}

// tlsConfig returns a *tls.Config when TLS is enabled, else nil.
func (c Config) tlsConfig() (*tls.Config, error) {
	if !c.TLS {
		return nil, nil
	}
	cfg := &tls.Config{}
	if c.TLSPin != "" {
		pin, err := parsePin(c.TLSPin)
		if err != nil {
			return nil, err
		}
		// The hub's certificate is self-signed, so chain-to-a-root and
		// name-matching cannot both be satisfied and are replaced here by an
		// exact-identity check. InsecureSkipVerify hands verification to
		// VerifyPeerCertificate, which FAILS CLOSED on any other certificate;
		// it is not "accept anything", and it is not reachable without a pin.
		// VerifyPeerCertificate runs even when HP_AGENT_TLS_INSECURE is set, so
		// a pinned agent cannot be downgraded by that escape hatch either.
		cfg.InsecureSkipVerify = true // #nosec G402 -- replaced by the pin check below
		cfg.VerifyPeerCertificate = pinVerifier(pin)
		log.Printf("hub certificate pinned to sha256:%x", pin)
	}
	if c.TLSCa != "" {
		// Explicit CA: pin verification to this pool.
		ca, err := os.ReadFile(c.TLSCa)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(ca)
		cfg.RootCAs = pool
	}
	// No CA given: leave RootCAs nil so Go verifies against the system trust
	// store. Verification stays ON by default. Only an explicit, truthy
	// HP_AGENT_TLS_INSECURE disables it — and that path is loudly logged.
	if envBool("HP_AGENT_TLS_INSECURE") {
		if cfg.VerifyPeerCertificate != nil {
			// A pin outranks the escape hatch: the pinned check still runs, so
			// the connection is NOT downgraded.
			log.Printf("WARNING: HP_AGENT_TLS_INSECURE ignored - the hub certificate pin still applies")
		} else {
			cfg.InsecureSkipVerify = true // #nosec G402 -- explicit operator override
			log.Printf("WARNING: TLS verification DISABLED via HP_AGENT_TLS_INSECURE")
		}
	}
	if c.TLSCert != "" && c.TLSKey != "" {
		cert, err := tls.LoadX509KeyPair(c.TLSCert, c.TLSKey)
		if err != nil {
			return nil, err
		}
		cfg.Certificates = []tls.Certificate{cert}
	}
	return cfg, nil
}
