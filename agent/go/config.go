package main

import (
	"crypto/tls"
	"crypto/x509"
	"log"
	"os"
	"strconv"
	"strings"
)

// Config mirrors hp_agent.config.AgentConfig (env-driven).
type Config struct {
	HubHost           string
	HubPort           int
	AuthToken         string
	AuthTokenFile     string
	AgentID           string
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
		AgentID:           os.Getenv("HP_AGENT_ID"),
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
