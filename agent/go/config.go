package main

import (
	"crypto/tls"
	"crypto/x509"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// defaultAgentIDFile is where the generated agent id is persisted when neither
// HP_AGENT_ID_FILE nor HP_AGENT_TOKEN_FILE gives a location to derive one from.
const defaultAgentIDFile = "/etc/homepilot/agent.id"

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
	Privileged        bool
	ZabbixEnabled     bool
	ZabbixServer      string
	ZabbixPort        int
	ZabbixHostname    string
	ZabbixInterval    int
	// HasPersistedToken is true when a durable per-agent token was loaded from
	// HP_AGENT_TOKEN_FILE. Such a token is a shared secret usable as the replay
	// MAC key, so the agent negotiates replay protection (#362 slice 3) on
	// connect. An agent enrolling for the first time (env/bootstrap token only)
	// has no per-agent key yet and does not negotiate.
	HasPersistedToken bool
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

// writeAgentIDFile writes id to path atomically with 0600 permissions (temp file
// in the same directory + rename), so a crash mid-write can never leave a
// truncated id behind.
func writeAgentIDFile(path, id string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".agent.id-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // no-op once the rename succeeded
	if err := f.Chmod(0o600); err != nil {
		_ = f.Close()
		return err
	}
	if _, err := f.WriteString(id); err != nil {
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
	return Config{
		HubHost:           envStr("HP_AGENT_HUB_HOST", "localhost"),
		HubPort:           envInt("HP_AGENT_HUB_PORT", 8443),
		AuthToken:         resolveToken(os.Getenv("HP_AGENT_TOKEN_FILE"), os.Getenv("HP_AGENT_AUTH_TOKEN")),
		AuthTokenFile:     os.Getenv("HP_AGENT_TOKEN_FILE"),
		EnvAuthToken:      os.Getenv("HP_AGENT_AUTH_TOKEN"),
		AgentID:           os.Getenv("HP_AGENT_ID"),
		AgentIDFile:       agentIDFilePath(os.Getenv("HP_AGENT_ID_FILE"), os.Getenv("HP_AGENT_TOKEN_FILE")),
		HeartbeatInterval: envInt("HP_AGENT_HEARTBEAT_INTERVAL", 30),
		LogLevel:          envStr("HP_AGENT_LOG_LEVEL", "INFO"),
		TLS:               envBool("HP_AGENT_TLS"),
		TLSCa:             os.Getenv("HP_AGENT_TLS_CA"),
		TLSCert:           os.Getenv("HP_AGENT_TLS_CERT"),
		TLSKey:            os.Getenv("HP_AGENT_TLS_KEY"),
		Privileged:        envBool("HP_AGENT_PRIVILEGED"),
		ZabbixEnabled:     envBool("HP_ZABBIX_ENABLED"),
		ZabbixServer:      envStr("HP_ZABBIX_SERVER", "localhost"),
		ZabbixPort:        envInt("HP_ZABBIX_PORT", 10051),
		ZabbixHostname:    os.Getenv("HP_ZABBIX_HOSTNAME"),
		ZabbixInterval:    envInt("HP_ZABBIX_SEND_INTERVAL", 60),
		HasPersistedToken: tokenFileHasContent(os.Getenv("HP_AGENT_TOKEN_FILE")),
	}
}

// tlsConfig returns a *tls.Config when TLS is enabled, else nil.
func (c Config) tlsConfig() (*tls.Config, error) {
	if !c.TLS {
		return nil, nil
	}
	cfg := &tls.Config{}
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
		cfg.InsecureSkipVerify = true
		log.Printf("WARNING: TLS verification DISABLED via HP_AGENT_TLS_INSECURE")
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
