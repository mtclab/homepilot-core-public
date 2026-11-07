package main

import (
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #683: the shared enrolment token must not stay on a host once that host holds
// its own credential.
//
// The installer writes HP_AGENT_AUTH_TOKEN (the fleet-wide shared token) into
// agent.env, and nothing ever removed it. Every enrolled host therefore kept a
// credential that could enrol ANY machine - and, before the hub-side half of
// #683, re-enrol a known hostname and take it over - long after the host itself
// had stopped needing it. These tests hold the agent to removing it the moment a
// working per-agent credential is persisted, and never before.

const sharedFleetToken = "shared-fleet-token-for-scrub-tests"

func writeEnvFile(t *testing.T, dir, token string) string {
	t.Helper()
	path := filepath.Join(dir, "agent.env")
	body := strings.Join([]string{
		"HP_AGENT_HUB_HOST=hub.example.com",
		"HP_AGENT_HUB_PORT=8443",
		"HP_AGENT_AUTH_TOKEN=" + token,
		"HP_AGENT_TOKEN_FILE=" + filepath.Join(dir, "agent.token"),
		"HP_AGENT_ID=agent-scrub",
		"",
	}, "\n")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func registerAgainst(t *testing.T, a *Agent, ack msg) {
	t.Helper()
	client, server := net.Pipe()
	t.Cleanup(func() { client.Close(); server.Close() })
	a.conn = client
	got := serveOnce(t, server, ack)
	if err := a.register(); err != nil {
		t.Fatalf("register failed: %v", err)
	}
	<-got
}

func TestFirstEnrolmentScrubsTheSharedTokenFromAgentEnv(t *testing.T) {
	dir := t.TempDir()
	envFile := writeEnvFile(t, dir, sharedFleetToken)
	tokenFile := filepath.Join(dir, "agent.token")
	a := &Agent{
		cfg: Config{
			AuthToken: sharedFleetToken, EnvAuthToken: sharedFleetToken,
			AuthTokenFile: tokenFile, EnvFile: envFile,
		},
		agentID: "agent-scrub",
	}

	registerAgainst(t, a, msg{
		"action": "register_ack", "agent_id": "agent-scrub", "auth_token": "minted-per-agent",
	})

	raw, err := os.ReadFile(envFile)
	if err != nil {
		t.Fatal(err)
	}
	env := string(raw)
	if strings.Contains(env, sharedFleetToken) || strings.Contains(env, "HP_AGENT_AUTH_TOKEN") {
		t.Fatalf("the shared enrolment token is still in agent.env after enrolment:\n%s", env)
	}
	for _, keep := range []string{"HP_AGENT_HUB_HOST=hub.example.com", "HP_AGENT_ID=agent-scrub",
		"HP_AGENT_TOKEN_FILE=" + tokenFile} {
		if !strings.Contains(env, keep) {
			t.Fatalf("scrubbing dropped an unrelated setting %q:\n%s", keep, env)
		}
	}
	if info, err := os.Stat(envFile); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("agent.env must stay 0600, got %v (%v)", info.Mode().Perm(), err)
	}
	if a.cfg.EnvAuthToken != "" {
		t.Fatalf("the shared token is still held in memory as the fallback credential")
	}
	if got := resolveToken(tokenFile, ""); got != "minted-per-agent" {
		t.Fatalf("per-agent credential not persisted: %q", got)
	}
}

// An upgraded fleet: the host enrolled long ago, holds a persisted credential,
// and agent.env still carries the shared token. The first successful per-agent
// register proves the credential works - that is when the token goes.
func TestAnUpgradedAgentScrubsOnItsFirstPerAgentRegister(t *testing.T) {
	dir := t.TempDir()
	envFile := writeEnvFile(t, dir, sharedFleetToken)
	tokenFile := filepath.Join(dir, "agent.token")
	if err := os.WriteFile(tokenFile, []byte("existing-per-agent"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{
		cfg: Config{
			AuthToken: "existing-per-agent", EnvAuthToken: sharedFleetToken,
			AuthTokenFile: tokenFile, EnvFile: envFile,
		},
		agentID: "agent-scrub", hasPerAgent: true, usingFileToken: true,
	}

	registerAgainst(t, a, msg{"action": "register_ack", "agent_id": "agent-scrub"})

	raw, _ := os.ReadFile(envFile)
	if strings.Contains(string(raw), sharedFleetToken) {
		t.Fatalf("an agent authenticated by its own credential kept the shared token:\n%s", raw)
	}
}

// Never before the credential is safely on disk: if the per-agent token could
// not be persisted, the shared token is the only way this host gets back after
// a restart, so removing it would strand the host.
func TestNoScrubWhenTheCredentialWasNotPersisted(t *testing.T) {
	dir := t.TempDir()
	envFile := writeEnvFile(t, dir, sharedFleetToken)
	// The token "file" sits under a regular file, so it can never be written.
	blocker := filepath.Join(dir, "not-a-dir")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &Agent{
		cfg: Config{
			AuthToken: sharedFleetToken, EnvAuthToken: sharedFleetToken,
			AuthTokenFile: filepath.Join(blocker, "agent.token"), EnvFile: envFile,
		},
		agentID: "agent-scrub",
	}

	registerAgainst(t, a, msg{
		"action": "register_ack", "agent_id": "agent-scrub", "auth_token": "minted-per-agent",
	})

	raw, _ := os.ReadFile(envFile)
	if !strings.Contains(string(raw), "HP_AGENT_AUTH_TOKEN="+sharedFleetToken) {
		t.Fatalf("the shared token was removed although the credential was never persisted:\n%s", raw)
	}
	if a.cfg.EnvAuthToken != sharedFleetToken {
		t.Fatalf("the in-memory fallback was dropped although nothing replaced it")
	}
}

// A line the agent did not enrol with is not the agent's to remove: an
// operator who put a NEW token there (to re-enrol after a revoke) keeps it.
func TestScrubOnlyRemovesTheTokenTheAgentEnrolledWith(t *testing.T) {
	dir := t.TempDir()
	envFile := writeEnvFile(t, dir, "a-token-the-operator-put-there-later")
	tokenFile := filepath.Join(dir, "agent.token")
	a := &Agent{
		cfg: Config{
			AuthToken: sharedFleetToken, EnvAuthToken: sharedFleetToken,
			AuthTokenFile: tokenFile, EnvFile: envFile,
		},
		agentID: "agent-scrub",
	}

	registerAgainst(t, a, msg{
		"action": "register_ack", "agent_id": "agent-scrub", "auth_token": "minted-per-agent",
	})

	raw, _ := os.ReadFile(envFile)
	if !strings.Contains(string(raw), "HP_AGENT_AUTH_TOKEN=a-token-the-operator-put-there-later") {
		t.Fatalf("scrub removed a token this agent did not enrol with:\n%s", raw)
	}
}

func TestEnvFilePathDerivesFromTheTokenFile(t *testing.T) {
	if got := envFilePath("", "/etc/homepilot/agent.token"); got != "/etc/homepilot/agent.env" {
		t.Fatalf("derived env file = %q", got)
	}
	if got := envFilePath("/srv/x.env", "/etc/homepilot/agent.token"); got != "/srv/x.env" {
		t.Fatalf("explicit env file ignored: %q", got)
	}
	// No token file => no persisted credential => nothing may be scrubbed.
	if got := envFilePath("", ""); got != "" {
		t.Fatalf("an agent with no token file must not scrub anything, got %q", got)
	}
}
