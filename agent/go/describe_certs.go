package main

import (
	"crypto/x509"
	"encoding/pem"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// TLS certificates (#818): names and expiry of the certificates nginx serves
// and trusts, and of every certbot lineage under /etc/letsencrypt/renewal.
// Only PEM blocks of type CERTIFICATE are ever decoded; any other block in a
// file (a private key bundled with its certificate) is skipped unread, and no
// file content is reported. A renewal configuration contributes its domains
// and authenticator/installer only - never the account, server or key paths.

const (
	maxCertificates = 200
	maxRenewals     = 200
	letsencryptDir  = "/etc/letsencrypt/renewal"
)

type certInfo struct {
	Path     string   `json:"path"`
	Source   string   `json:"source"`
	Status   string   `json:"status"`
	Reason   string   `json:"reason,omitempty"`
	Subject  string   `json:"subject,omitempty"`
	Names    []string `json:"names,omitempty"`
	Issuer   string   `json:"issuer,omitempty"`
	NotAfter string   `json:"not_after,omitempty"`
	DaysLeft *int     `json:"days_left,omitempty"`
}

type renewalInfo struct {
	Name          string   `json:"name"`
	Domains       []string `json:"domains"`
	Authenticator string   `json:"authenticator,omitempty"`
	Installer     string   `json:"installer,omitempty"`
	Certificate   string   `json:"certificate,omitempty"`
}

type certsSection struct {
	Status       string        `json:"status"`
	Reason       string        `json:"reason,omitempty"`
	Certificates []certInfo    `json:"certificates"`
	Letsencrypt  []renewalInfo `json:"letsencrypt"`
}

func collectCertificates(env describeEnv, nginxPaths []string) certsSection {
	sec := certsSection{Status: statusOK, Certificates: []certInfo{}, Letsencrypt: []renewalInfo{}}
	sources := map[string]string{}
	for _, p := range nginxPaths {
		sources[p] = "nginx"
	}

	renewals, err := os.ReadDir(env.hostPath(letsencryptDir))
	switch {
	case err == nil:
		for _, e := range renewals {
			if !strings.HasSuffix(e.Name(), ".conf") || len(sec.Letsencrypt) >= maxRenewals {
				continue
			}
			r, ok := parseRenewal(env, letsencryptDir+"/"+e.Name())
			if !ok {
				continue
			}
			sec.Letsencrypt = append(sec.Letsencrypt, r)
			if r.Certificate != "" {
				if _, known := sources[r.Certificate]; !known {
					sources[r.Certificate] = "letsencrypt"
				}
			}
		}
	case os.IsPermission(err):
		sec.Status, sec.Reason = statusPartial, "certbot renewal configurations are "+notVisibleReason
	}

	paths := make([]string, 0, len(sources))
	for p := range sources {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	if len(paths) > maxCertificates {
		paths = paths[:maxCertificates]
	}
	hidden := 0
	for _, p := range paths {
		c := readCertificate(env, p)
		c.Source = sources[p]
		if c.Status == statusNotVisible {
			hidden++
		}
		sec.Certificates = append(sec.Certificates, c)
	}
	if hidden > 0 && sec.Status == statusOK {
		sec.Status = statusPartial
		sec.Reason = "some certificate files are " + notVisibleReason
	}
	return sec
}

// readCertificate decodes the first CERTIFICATE block of a file.
func readCertificate(env describeEnv, p string) certInfo {
	c := certInfo{Path: p}
	if !strings.HasPrefix(p, "/") {
		c.Status, c.Reason = statusError, "a relative path (nginx resolves it against its prefix)"
		return c
	}
	raw, err := env.readHostFile(p)
	if err != nil {
		c.Status, c.Reason = classifyErr(err)
		if c.Status == statusNotInstalled {
			c.Reason = "the file does not exist"
		}
		return c
	}
	rest := raw
	for {
		var block *pem.Block
		block, rest = pem.Decode(rest)
		if block == nil {
			c.Status, c.Reason = statusError, "no certificate in the file"
			return c
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			c.Status, c.Reason = statusError, "the certificate could not be parsed"
			return c
		}
		c.Status = statusOK
		c.Subject = cert.Subject.CommonName
		c.Issuer = cert.Issuer.CommonName
		c.Names = append([]string{}, cert.DNSNames...)
		c.NotAfter = cert.NotAfter.UTC().Format(time.RFC3339)
		days := int(cert.NotAfter.Sub(env.now()).Hours() / 24)
		c.DaysLeft = &days
		return c
	}
}

// parseRenewal reads a certbot renewal configuration: the lineage name, its
// certificate path, the authenticator/installer and the webroot domains. Every
// other key - account, server, key paths, credentials files - is ignored.
func parseRenewal(env describeEnv, p string) (renewalInfo, bool) {
	raw, err := env.readHostFile(p)
	if err != nil {
		return renewalInfo{}, false
	}
	r := renewalInfo{Name: strings.TrimSuffix(filepath.Base(p), ".conf"), Domains: []string{}}
	section := ""
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.HasPrefix(line, "[") {
			section = strings.Trim(line, "[]")
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k, v = strings.TrimSpace(k), strings.TrimSpace(v)
		switch {
		case section == "" && k == "cert":
			r.Certificate = v
		case section == "renewalparams" && k == "authenticator":
			r.Authenticator = v
		case section == "renewalparams" && k == "installer":
			r.Installer = v
		case section == "webroot_map":
			r.Domains = append(r.Domains, k)
		}
	}
	if len(r.Domains) == 0 {
		// Not a webroot lineage: the lineage name is the certificate's first
		// domain by certbot's convention; the certificate's names say the rest.
		r.Domains = []string{r.Name}
	}
	sort.Strings(r.Domains)
	return r, true
}
