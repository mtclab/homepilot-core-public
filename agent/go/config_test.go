package main

import "testing"

// #377: TLS verification must be ON by default. With TLS enabled but no CA and
// no HP_AGENT_TLS_INSECURE, the returned config must NOT skip verification and
// must leave RootCAs nil so Go falls back to the system trust store.
func TestTLSConfigVerifiesByDefault(t *testing.T) {
	t.Setenv("HP_AGENT_TLS_INSECURE", "")
	c := Config{TLS: true}
	cfg, err := c.tlsConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil tls config when TLS enabled")
	}
	if cfg.InsecureSkipVerify {
		t.Fatal("InsecureSkipVerify must be false by default (no CA, no HP_AGENT_TLS_INSECURE)")
	}
	if cfg.RootCAs != nil {
		t.Fatal("RootCAs must be nil so the system trust store is used")
	}
}

// #377: the only way to disable verification is the explicit escape hatch.
func TestTLSConfigInsecureEscapeHatch(t *testing.T) {
	t.Setenv("HP_AGENT_TLS_INSECURE", "1")
	c := Config{TLS: true}
	cfg, err := c.tlsConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil tls config when TLS enabled")
	}
	if !cfg.InsecureSkipVerify {
		t.Fatal("HP_AGENT_TLS_INSECURE=1 must set InsecureSkipVerify=true")
	}
}
