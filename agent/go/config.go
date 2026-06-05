package main

import (
	"crypto/tls"
	"crypto/x509"
	"os"
	"strconv"
	"strings"
)

// Config mirrors hp_agent.config.AgentConfig (env-driven).
type Config struct {
	HubHost           string
	HubPort           int
	AuthToken         string
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
		AuthToken:         os.Getenv("HP_AGENT_AUTH_TOKEN"),
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
	}
}

// tlsConfig returns a *tls.Config when TLS is enabled, else nil.
func (c Config) tlsConfig() (*tls.Config, error) {
	if !c.TLS {
		return nil, nil
	}
	cfg := &tls.Config{}
	if c.TLSCa != "" {
		ca, err := os.ReadFile(c.TLSCa)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(ca)
		cfg.RootCAs = pool
	} else {
		cfg.InsecureSkipVerify = true // mirrors Python CERT_NONE when no CA given
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
