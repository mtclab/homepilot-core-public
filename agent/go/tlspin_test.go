package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"testing"
	"time"
)

// selfSigned builds a throwaway self-signed certificate shaped like the one the
// hub generates for itself, and returns it with its sha256-over-DER pin.
func selfSigned(t *testing.T) (tls.Certificate, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(time.Now().UnixNano()),
		Subject:               pkix.Name{CommonName: "homepilot-agent-hub"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  true,
		DNSNames:              []string{"localhost"},
		IPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("createcert: %v", err)
	}
	sum := sha256.Sum256(der)
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parsecert: %v", err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key, Leaf: leaf},
		"sha256:" + hexOf(sum[:])
}

func hexOf(b []byte) string {
	const digits = "0123456789abcdef"
	out := make([]byte, 0, len(b)*2)
	for _, c := range b {
		out = append(out, digits[c>>4], digits[c&0x0f])
	}
	return string(out)
}

// serveTLS starts a one-shot TLS listener presenting cert and returns its addr.
func serveTLS(t *testing.T, cert tls.Certificate) string {
	t.Helper()
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			// Force the handshake so the client sees a real server, then drop it.
			if tc, ok := conn.(*tls.Conn); ok {
				_ = tc.Handshake()
			}
			_ = conn.Close()
		}
	}()
	return ln.Addr().String()
}

func dialPinned(t *testing.T, addr, pin string) error {
	t.Helper()
	agentCfg := Config{TLS: true, TLSPin: pin}
	cfg, err := agentCfg.tlsConfig()
	if err != nil {
		return err
	}
	conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", addr, cfg)
	if err != nil {
		return err
	}
	_ = conn.Close()
	return nil
}

// The point of the pin: the agent completes a handshake with the hub it was told
// to trust and REFUSES any other certificate - including another perfectly
// well-formed self-signed one, which is exactly what an impostor hub would
// present.
func TestPinnedAgentAcceptsOnlyTheHubItWasGiven(t *testing.T) {
	hubCert, hubPin := selfSigned(t)
	impostorCert, impostorPin := selfSigned(t)
	if hubPin == impostorPin {
		t.Fatal("test fixtures collided on one fingerprint")
	}

	hubAddr := serveTLS(t, hubCert)
	if err := dialPinned(t, hubAddr, hubPin); err != nil {
		t.Fatalf("pinned agent must connect to its own hub: %v", err)
	}

	impostorAddr := serveTLS(t, impostorCert)
	if err := dialPinned(t, impostorAddr, hubPin); err == nil {
		t.Fatal("pinned agent connected to a hub presenting a DIFFERENT certificate")
	}
}

// The insecure escape hatch must not be a way around a pin.
func TestPinSurvivesInsecureEscapeHatch(t *testing.T) {
	hubCert, hubPin := selfSigned(t)
	impostorCert, _ := selfSigned(t)
	t.Setenv("HP_AGENT_TLS_INSECURE", "1")

	if err := dialPinned(t, serveTLS(t, hubCert), hubPin); err != nil {
		t.Fatalf("pinned agent must still reach its own hub: %v", err)
	}
	if err := dialPinned(t, serveTLS(t, impostorCert), hubPin); err == nil {
		t.Fatal("HP_AGENT_TLS_INSECURE bypassed the certificate pin")
	}
}

// An unpinned agent must not be talked into trusting a self-signed hub: with no
// pin and no CA, verification is the system trust store and this must fail.
func TestUnpinnedAgentRejectsSelfSignedHub(t *testing.T) {
	hubCert, _ := selfSigned(t)
	if err := dialPinned(t, serveTLS(t, hubCert), ""); err == nil {
		t.Fatal("agent with no pin and no CA accepted a self-signed hub certificate")
	}
}

func TestPinParsingRejectsGarbage(t *testing.T) {
	for _, bad := range []string{"sha256:zz", "abcd", "sha256:" + hexOf(make([]byte, 31))} {
		cfg := Config{TLS: true, TLSPin: bad}
		if _, err := cfg.tlsConfig(); err == nil {
			t.Fatalf("pin %q must be rejected, not silently ignored", bad)
		}
	}
}

func TestPinAcceptsHexWithoutPrefixAndWithColons(t *testing.T) {
	hubCert, hubPin := selfSigned(t)
	addr := serveTLS(t, hubCert)
	bare := hubPin[len("sha256:"):]
	if err := dialPinned(t, addr, bare); err != nil {
		t.Fatalf("bare hex pin must work: %v", err)
	}
	colonised := ""
	for i := 0; i < len(bare); i += 2 {
		if i > 0 {
			colonised += ":"
		}
		colonised += bare[i : i+2]
	}
	if err := dialPinned(t, addr, colonised); err != nil {
		t.Fatalf("colon-separated pin must work: %v", err)
	}
}
