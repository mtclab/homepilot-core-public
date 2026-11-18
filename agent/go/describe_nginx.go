package main

import (
	"errors"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

// nginx (#818): the server blocks of the ACTIVE configuration - nginx.conf and
// everything it includes - parsed by hand (a small tokenizer, no dependency).
// Reported per server: server_name, listen, root, the TLS files and client-
// certificate mode, and per location the proxy/root/alias/return target, plus
// the upstreams and the include graph. Output is allowlisted directive by
// directive: proxy_set_header keeps its VALUE only for a known-safe header, and
// a directive whose name looks like a key/secret/token/password is never
// reported (ssl_certificate_key is reported as "present", never as its path).

const (
	nginxConfRoot      = "/etc/nginx"
	nginxMainConf      = "/etc/nginx/nginx.conf"
	maxNginxFiles      = 200
	maxNginxDepth      = 8
	maxNginxServers    = 300
	maxNginxLocations  = 100
	maxNginxUpstreams  = 100
	maxNginxHeaderRows = 40
)

// safeHeaders keep their values: they carry request metadata ($host,
// $remote_addr, $scheme, ...), not credentials.
var safeHeaders = map[string]bool{
	"host": true, "x-real-ip": true, "x-forwarded-for": true, "x-forwarded-proto": true,
	"x-forwarded-host": true, "x-forwarded-port": true, "x-forwarded-prefix": true,
	"x-forwarded-ssl": true, "x-forwarded-scheme": true, "x-scheme": true, "forwarded": true,
	"upgrade": true, "connection": true, "x-ssl-client-verify": true, "x-client-verify": true,
	"x-ssl-client-s-dn": true, "x-original-uri": true, "x-original-url": true,
}

type nginxNode struct {
	name     string
	args     []string
	children []*nginxNode
	block    bool
	file     string
}

type nginxHeader struct {
	Name         string `json:"name"`
	Value        string `json:"value,omitempty"`
	ValueDropped bool   `json:"value_dropped,omitempty"`
}

type nginxLocation struct {
	Match     string        `json:"match"`
	ProxyPass string        `json:"proxy_pass,omitempty"`
	Upstream  string        `json:"pass,omitempty"`
	Root      string        `json:"root,omitempty"`
	Alias     string        `json:"alias,omitempty"`
	Return    string        `json:"return,omitempty"`
	Headers   []nginxHeader `json:"headers,omitempty"`
}

type nginxTLS struct {
	Certificate       string `json:"certificate,omitempty"`
	CertificateKey    string `json:"certificate_key,omitempty"`
	ClientCertificate string `json:"client_certificate,omitempty"`
	VerifyClient      string `json:"verify_client,omitempty"`
}

type nginxServer struct {
	Context     string          `json:"context"`
	File        string          `json:"file"`
	ServerNames []string        `json:"server_names"`
	Listen      []string        `json:"listen"`
	Root        string          `json:"root,omitempty"`
	Return      string          `json:"return,omitempty"`
	ProxyPass   string          `json:"proxy_pass,omitempty"`
	TLS         *nginxTLS       `json:"tls,omitempty"`
	Headers     []nginxHeader   `json:"headers,omitempty"`
	Locations   []nginxLocation `json:"locations"`
}

type nginxUpstream struct {
	Name    string   `json:"name"`
	File    string   `json:"file"`
	Servers []string `json:"servers"`
}

type nginxInclude struct {
	File     string   `json:"file"`
	Includes []string `json:"includes"`
}

type nginxSection struct {
	Status    string          `json:"status"`
	Reason    string          `json:"reason,omitempty"`
	Config    string          `json:"config,omitempty"`
	Files     []nginxInclude  `json:"files"`
	Servers   []nginxServer   `json:"servers"`
	Upstreams []nginxUpstream `json:"upstreams"`

	certPaths []string
}

// certificatePaths are the certificate files the configuration references,
// for the certificates collector.
func (s nginxSection) certificatePaths() []string { return s.certPaths }

type nginxParser struct {
	env        describeEnv
	files      map[string][]string
	order      []string
	unreadable int
}

func collectNginx(env describeEnv) nginxSection {
	sec := nginxSection{Files: []nginxInclude{}, Servers: []nginxServer{}, Upstreams: []nginxUpstream{}}
	p := &nginxParser{env: env, files: map[string][]string{}}
	root, err := p.parseFile(nginxMainConf, 0)
	if err != nil {
		sec.Status, sec.Reason = classifyErr(err)
		return sec
	}
	sec.Status, sec.Config = statusOK, nginxMainConf
	if p.unreadable > 0 {
		sec.Status = statusPartial
		sec.Reason = "some included files are " + notVisibleReason
	}
	for _, f := range p.order {
		sec.Files = append(sec.Files, nginxInclude{File: f, Includes: p.files[f]})
	}
	certs := map[string]bool{}
	var walk func(nodes []*nginxNode, ctx string)
	walk = func(nodes []*nginxNode, ctx string) {
		for _, n := range nodes {
			switch {
			case n.block && n.name == "server" && (ctx == "http" || ctx == "stream"):
				if len(sec.Servers) < maxNginxServers {
					sec.Servers = append(sec.Servers, nginxServerOf(n, ctx, certs))
				}
			case n.block && n.name == "upstream":
				if len(sec.Upstreams) < maxNginxUpstreams {
					sec.Upstreams = append(sec.Upstreams, nginxUpstreamOf(n))
				}
			case n.block && (n.name == "http" || n.name == "stream"):
				walk(n.children, n.name)
			}
		}
	}
	walk(root, "main")
	for c := range certs {
		sec.certPaths = append(sec.certPaths, c)
	}
	sort.Strings(sec.certPaths)
	return sec
}

func nginxServerOf(n *nginxNode, ctx string, certs map[string]bool) nginxServer {
	s := nginxServer{Context: ctx, File: n.file, ServerNames: []string{}, Listen: []string{}, Locations: []nginxLocation{}}
	tls := &nginxTLS{}
	for _, d := range n.children {
		if looksSecretName(d.name) && d.name != "ssl_certificate_key" {
			continue
		}
		switch d.name {
		case "server_name":
			s.ServerNames = append(s.ServerNames, d.args...)
		case "listen":
			s.Listen = append(s.Listen, strings.Join(d.args, " "))
		case "root":
			s.Root = first(d.args)
		case "return":
			s.Return = strings.Join(d.args, " ")
		case "proxy_pass":
			s.ProxyPass = first(d.args)
		case "ssl_certificate":
			tls.Certificate = first(d.args)
			certs[tls.Certificate] = true
		case "ssl_certificate_key":
			// Never the path of a private key: only that one is configured.
			tls.CertificateKey = "present"
		case "ssl_client_certificate", "ssl_trusted_certificate":
			if d.name == "ssl_client_certificate" {
				tls.ClientCertificate = first(d.args)
			}
			certs[first(d.args)] = true
		case "ssl_verify_client":
			tls.VerifyClient = first(d.args)
		case "proxy_set_header":
			if len(s.Headers) < maxNginxHeaderRows {
				s.Headers = append(s.Headers, nginxHeaderOf(d.args))
			}
		case "location":
			collectLocations(d, &s.Locations)
		}
	}
	if *tls != (nginxTLS{}) {
		s.TLS = tls
	}
	return s
}

func collectLocations(n *nginxNode, out *[]nginxLocation) {
	if !n.block || len(*out) >= maxNginxLocations {
		return
	}
	loc := nginxLocation{Match: strings.Join(n.args, " ")}
	var nested []*nginxNode
	for _, d := range n.children {
		if looksSecretName(d.name) {
			continue
		}
		switch d.name {
		case "proxy_pass":
			loc.ProxyPass = first(d.args)
		case "fastcgi_pass", "uwsgi_pass", "grpc_pass", "scgi_pass":
			loc.Upstream = d.name + " " + first(d.args)
		case "root":
			loc.Root = first(d.args)
		case "alias":
			loc.Alias = first(d.args)
		case "return":
			loc.Return = strings.Join(d.args, " ")
		case "proxy_set_header":
			if len(loc.Headers) < maxNginxHeaderRows {
				loc.Headers = append(loc.Headers, nginxHeaderOf(d.args))
			}
		case "location":
			nested = append(nested, d)
		}
	}
	*out = append(*out, loc)
	for _, d := range nested {
		collectLocations(d, out)
	}
}

// nginxHeaderOf keeps a header's name always and its value only when the header
// is on the safe list and its name does not look like a credential.
func nginxHeaderOf(args []string) nginxHeader {
	h := nginxHeader{Name: first(args)}
	value := strings.Join(args[min(1, len(args)):], " ")
	if safeHeaders[strings.ToLower(h.Name)] && !looksSecretName(h.Name) {
		h.Value = value
	} else if value != "" {
		h.ValueDropped = true
	}
	return h
}

func nginxUpstreamOf(n *nginxNode) nginxUpstream {
	u := nginxUpstream{Name: first(n.args), File: n.file, Servers: []string{}}
	for _, d := range n.children {
		if d.name == "server" && !d.block {
			u.Servers = append(u.Servers, first(d.args))
		}
	}
	return u
}

func first(args []string) string {
	if len(args) == 0 {
		return ""
	}
	return args[0]
}

// parseFile parses one configuration file and splices its includes in place.
func (p *nginxParser) parseFile(file string, depth int) ([]*nginxNode, error) {
	if _, seen := p.files[file]; seen || len(p.order) >= maxNginxFiles {
		return nil, nil
	}
	raw, err := p.env.readHostFile(file)
	if err != nil {
		return nil, err
	}
	p.files[file] = []string{}
	p.order = append(p.order, file)
	nodes, _ := parseNginx(tokenizeNginx(string(raw)), file)
	return p.expand(nodes, file, depth), nil
}

func (p *nginxParser) expand(nodes []*nginxNode, file string, depth int) []*nginxNode {
	var out []*nginxNode
	for _, n := range nodes {
		if n.name == "include" && !n.block && len(n.args) == 1 && depth < maxNginxDepth {
			pattern := n.args[0]
			if !strings.HasPrefix(pattern, "/") {
				pattern = path.Join(nginxConfRoot, pattern)
			}
			matches, _ := filepath.Glob(p.env.hostPath(pattern))
			sort.Strings(matches)
			for _, m := range matches {
				rel, err := filepath.Rel(p.env.root, m)
				if err != nil {
					continue
				}
				hostFile := "/" + filepath.ToSlash(rel)
				p.files[file] = append(p.files[file], hostFile)
				included, err := p.parseFile(hostFile, depth+1)
				if errors.Is(err, os.ErrPermission) {
					p.unreadable++
				}
				out = append(out, included...)
			}
			continue
		}
		if n.block {
			n.children = p.expand(n.children, file, depth)
		}
		out = append(out, n)
	}
	return out
}

// tokenizeNginx splits a configuration into words, quoted strings and the
// punctuation { } ;. Comments run from # to the end of the line.
func tokenizeNginx(s string) []string {
	var toks []string
	var cur strings.Builder
	flush := func() {
		if cur.Len() > 0 {
			toks = append(toks, cur.String())
			cur.Reset()
		}
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '#':
			flush()
			for i < len(s) && s[i] != '\n' {
				i++
			}
		case c == '"' || c == '\'':
			flush()
			quote := c
			i++
			for i < len(s) && s[i] != quote {
				if s[i] == '\\' && i+1 < len(s) {
					i++
				}
				cur.WriteByte(s[i])
				i++
			}
			toks = append(toks, cur.String())
			cur.Reset()
		case c == '{' || c == '}' || c == ';':
			flush()
			toks = append(toks, string(c))
		case c == ' ' || c == '\t' || c == '\n' || c == '\r':
			flush()
		default:
			cur.WriteByte(c)
		}
	}
	flush()
	return toks
}

// parseNginx builds the directive tree from tokens. It is lenient: a malformed
// file yields what could be parsed, never an error that hides the rest.
func parseNginx(toks []string, file string) ([]*nginxNode, int) {
	var out []*nginxNode
	i := 0
	for i < len(toks) {
		if toks[i] == "}" {
			return out, i + 1
		}
		if toks[i] == ";" || toks[i] == "{" {
			i++
			continue
		}
		n := &nginxNode{name: toks[i], file: file}
		i++
		for i < len(toks) && toks[i] != ";" && toks[i] != "{" && toks[i] != "}" {
			n.args = append(n.args, toks[i])
			i++
		}
		if i < len(toks) && toks[i] == "{" {
			n.block = true
			children, used := parseNginx(toks[i+1:], file)
			n.children = children
			i += 1 + used
		} else if i < len(toks) && toks[i] == ";" {
			i++
		}
		out = append(out, n)
	}
	return out, i
}
