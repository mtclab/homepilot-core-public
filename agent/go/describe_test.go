package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// #818: the host description, against a fixture filesystem and canned command
// and Docker API answers. The secrets sentinel tests are the point: every place
// a real host keeps a credential near build-defining state - an env file, a
// private key, a bearer header in nginx, a password in a container's
// environment, a token in a unit's argv, a credential in a URL, a label - holds
// a unique marker, and the finished description must contain none of them.

// The sentinels are assembled from fragments so no literal credential-shaped
// string sits in the tree.
func sentinel(name string) string { return "hpSENTINEL" + "818" + name + "zq" }

var sentinelNames = []string{
	"envfile", "keyfile", "bearerhdr", "cookiehdr", "composeenv", "ctrcmd", "label",
	"unitargv", "unitenv", "proxyuser", "returnbearer", "renewacct", "cmdline", "apikeyhdr",
	// #853: a unit's EnvironmentFile= target and a drop-in's Environment=.
	"unitenvfile", "unitdropin",
}

var fixtureNow = time.Date(2026, 11, 18, 12, 0, 0, 0, time.UTC)

func writeFixture(t *testing.T, root, p, content string) {
	t.Helper()
	full := filepath.Join(root, p)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// selfSignedPEM makes a certificate for the fixture's nginx to serve.
func selfSignedPEM(t *testing.T, cn string, names []string, notAfter time.Time) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(818),
		Subject:      pkix.Name{CommonName: cn},
		DNSNames:     names,
		NotBefore:    fixtureNow.Add(-24 * time.Hour),
		NotAfter:     notAfter,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

func fakePrivateKey(marker string) string {
	return "-----BEGIN " + "PRIVATE KEY-----\n" + marker + "\n-----END " + "PRIVATE KEY-----\n"
}

// buildFixtureHost lays out a reverse-proxy host: nginx with two vhosts and an
// upstream, certificates, certbot, ufw, update-notifier, /proc sockets.
func buildFixtureHost(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	writeFixture(t, root, "etc/hostname", "proxy-1\n")
	writeFixture(t, root, "etc/os-release", "NAME=\"Ubuntu\"\nPRETTY_NAME=\"Ubuntu 24.04.1 LTS\"\n")
	writeFixture(t, root, "proc/sys/kernel/osrelease", "6.8.0-48-generic\n")
	writeFixture(t, root, "etc/passwd", "root:x:0:0:root:/root:/bin/bash\nwww-data:x:33:33::/var/www:/usr/sbin/nologin\nhp-agent:x:999:999::/nonexistent:/usr/sbin/nologin\n")

	// Secrets that must never be read into the description at all.
	writeFixture(t, root, "opt/app/.env", "API_TOKEN="+sentinel("envfile")+"\n")
	writeFixture(t, root, "etc/ssl/private/site.key", fakePrivateKey(sentinel("keyfile")))
	// #853: a drop-in holding a secret, and the file a unit loads its
	// environment from. The units collector reads neither.
	writeFixture(t, root, "etc/systemd/system/app.service.d/override.conf",
		"[Service]\nEnvironment=TOKEN="+sentinel("unitdropin")+"\nEnvironmentFile=/etc/default/app-settings\n")
	writeFixture(t, root, "etc/default/app-settings", "KEY="+sentinel("unitenvfile")+"\n")

	// Sockets: 443 and 80 owned by nginx (fd visible), 53/udp unowned.
	writeFixture(t, root, "proc/net/tcp",
		"  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"+
			"   0: 00000000:01BB 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1001 1 0\n"+
			"   1: 0100007F:1538 00000000:0000 0A 00000000:00000000 00:00000000 00000000   999        0 1004 1 0\n"+
			"   2: 0100007F:1538 0100007F:9C40 01 00000000:00000000 00:00000000 00000000   999        0 1005 1 0\n")
	writeFixture(t, root, "proc/net/tcp6",
		"  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"+
			"   0: 00000000000000000000000000000000:0050 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1002 1 0\n")
	writeFixture(t, root, "proc/net/udp",
		"  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n"+
			"   0: 3500007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000   101        0 1003 2 0 0\n")
	writeFixture(t, root, "proc/123/comm", "nginx\n")
	writeFixture(t, root, "proc/123/cgroup", "0::/system.slice/nginx.service\n")
	writeFixture(t, root, "proc/123/cmdline", "nginx\x00--secret\x00"+sentinel("cmdline"))
	if err := os.MkdirAll(filepath.Join(root, "proc/123/fd"), 0o755); err != nil {
		t.Fatal(err)
	}
	for fd, inode := range map[string]string{"3": "1001", "4": "1002", "5": "9999"} {
		if err := os.Symlink("socket:["+inode+"]", filepath.Join(root, "proc/123/fd", fd)); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("/dev/null", filepath.Join(root, "proc/123/fd/0")); err != nil {
		t.Fatal(err)
	}

	// nginx.
	writeFixture(t, root, "etc/nginx/nginx.conf", `
user www-data;   # the workers' user
events { worker_connections 768; }
http {
    include /etc/nginx/mime.types;
    include /etc/nginx/conf.d/*.conf;
    include sites-enabled/*;
}
stream {
    server { listen 5432; proxy_pass 10.0.0.20:5432; }
}
`)
	writeFixture(t, root, "etc/nginx/mime.types", "types { text/html html; }\n")
	writeFixture(t, root, "etc/nginx/conf.d/upstreams.conf", `
upstream app_backend {
    server 10.0.0.10:8080;
    server 10.0.0.11:8080 backup;
}
`)
	writeFixture(t, root, "etc/nginx/sites-enabled/estate.conf", `
server {
    listen 80;
    listen [::]:80;
    server_name example.com www.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name "example.com";
    root /var/www/example;
    ssl_certificate /etc/ssl/certs/example.pem;
    ssl_certificate_key /etc/ssl/private/site.key;
    ssl_client_certificate /etc/nginx/client-ca.pem;
    ssl_verify_client optional;
    ssl_password_file /etc/nginx/`+sentinel("cmdline")+`.pass;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Authorization "Bearer `+sentinel("bearerhdr")+`";
    location / {
        proxy_pass http://app_backend;
        proxy_set_header Cookie "session=`+sentinel("cookiehdr")+`";
        proxy_set_header X-Api-Key `+sentinel("apikeyhdr")+`;
        location /static/ { alias /srv/static/; }
    }
    location /legacy {
        proxy_pass https://svc:`+sentinel("proxyuser")+`@10.0.0.30:8443;
    }
    location = /probe { return 200 "Bearer `+sentinel("returnbearer")+`"; }
}
`)
	writeFixture(t, root, "etc/ssl/certs/example.pem", selfSignedPEM(t, "example.com", []string{"example.com", "www.example.com"}, fixtureNow.Add(30*24*time.Hour)))
	writeFixture(t, root, "etc/nginx/client-ca.pem", selfSignedPEM(t, "Estate Client CA", nil, fixtureNow.Add(3650*24*time.Hour)))

	writeFixture(t, root, "etc/letsencrypt/renewal/example.org.conf", `# renew_before_expiry = 30 days
version = 2.9.0
archive_dir = /etc/letsencrypt/archive/example.org
cert = /etc/letsencrypt/live/example.org/cert.pem
privkey = /etc/letsencrypt/live/example.org/privkey.pem
[renewalparams]
account = `+sentinel("renewacct")+`
authenticator = webroot
installer = nginx
server = https://acme-v02.api.letsencrypt.org/directory
[[webroot_map]]
example.org = /var/www/example
www.example.org = /var/www/example
`)

	writeFixture(t, root, "etc/ufw/ufw.conf", "# ufw\nENABLED=yes\nLOGLEVEL=low\n")
	writeFixture(t, root, "etc/ufw/user.rules", "*filter\n### tuple ### allow tcp 22 0.0.0.0/0 any 10.0.0.0/8 in\n-A ufw-user-input -p tcp --dport 22 -s 10.0.0.0/8 -j ACCEPT\n### tuple ### allow tcp 443 0.0.0.0/0 any 0.0.0.0/0 in comment=7765620a\nCOMMIT\n")
	writeFixture(t, root, "etc/ufw/user6.rules", "*filter\n### tuple ### allow tcp 443 ::/0 any ::/0 in\nCOMMIT\n")
	writeFixture(t, root, "var/lib/update-notifier/updates-available", "\n7 updates can be applied immediately.\n3 of these updates are standard security updates.\nTo see these additional updates run: apt list --upgradable\n")
	writeFixture(t, root, "run/reboot-required", "*** System restart required ***\n")
	return root
}

// fixtureCommands answers the commands the collectors run.
func fixtureCommands(overrides map[string]func() (string, error)) func(ctx context.Context, name string, args ...string) (string, error) {
	return func(_ context.Context, name string, args ...string) (string, error) {
		key := name + " " + strings.Join(args, " ")
		for prefix, f := range overrides {
			if strings.HasPrefix(key, prefix) {
				return f()
			}
		}
		switch {
		case strings.HasPrefix(key, "systemctl list-units"):
			return "nginx.service loaded active running A high performance web server and a reverse proxy server\n" +
				"app.service loaded active running app --token=" + sentinel("unitargv") + "\n" +
				"certbot.timer loaded active waiting Run certbot twice daily\n" +
				"apt-daily.service loaded inactive dead Daily apt download activities\n" +
				"ghost.service not-found inactive dead ghost.service\n", nil
		case strings.HasPrefix(key, "systemctl list-unit-files"):
			return "nginx.service enabled enabled\napp.service enabled enabled\ncertbot.timer enabled enabled\n" +
				"backup.service enabled enabled\napt-daily.service static -\ngetty@.service enabled enabled\n", nil
		case strings.HasPrefix(key, "systemctl show"):
			var b strings.Builder
			for _, unit := range args {
				switch unit {
				case "nginx.service":
					b.WriteString("Id=nginx.service\nExecStart={ path=/usr/sbin/nginx ; argv[]=/usr/sbin/nginx -g daemon on; master_process on; ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }\nFragmentPath=/lib/systemd/system/nginx.service\nUnitFileState=enabled\n\n")
				case "app.service":
					b.WriteString("Id=app.service\nExecStart={ path=/opt/app/bin/app ; argv[]=/opt/app/bin/app --token=" + sentinel("unitargv") + " ; ignore_errors=no }\nEnvironment=DB_PASSWORD=" + sentinel("unitenv") + "\nEnvironmentFiles=/etc/default/app-" + sentinel("unitenvfile") + " (ignore_errors=no)\nDropInPaths=/etc/systemd/system/app.service.d/override.conf\nFragmentPath=/etc/systemd/system/app.service\nUnitFileState=enabled\n\n")
				case "backup.service":
					b.WriteString("Id=backup.service\nExecStart={ path=/usr/local/bin/backup ; argv[]=/usr/local/bin/backup --to s3 ; ignore_errors=no } ; { path=/usr/bin/true ; argv[]=/usr/bin/true ; ignore_errors=no }\nFragmentPath=/etc/systemd/system/backup.service\nUnitFileState=enabled\n\n")
				case "certbot.timer":
					b.WriteString("Id=certbot.timer\nExecStart=\nFragmentPath=/lib/systemd/system/certbot.timer\nUnitFileState=enabled\n\n")
				}
			}
			return b.String(), nil
		case key == "timedatectl show":
			return "Timezone=America/Chicago\nLocalRTC=no\nCanNTP=yes\nNTP=yes\nNTPSynchronized=yes\n", nil
		case key == "nft -j list ruleset":
			return `{"nftables":[{"metainfo":{"version":"1.0.9"}},` +
				`{"table":{"family":"inet","name":"filter","handle":1}},` +
				`{"chain":{"family":"inet","table":"filter","name":"input","handle":1,"type":"filter","hook":"input","prio":0,"policy":"drop"}},` +
				`{"chain":{"family":"inet","table":"filter","name":"forward","handle":2,"type":"filter","hook":"forward","prio":0,"policy":"accept"}},` +
				`{"rule":{"family":"inet","table":"filter","chain":"input","handle":3,"expr":[]}},` +
				`{"rule":{"family":"inet","table":"filter","chain":"input","handle":4,"expr":[]}}]}`, nil
		}
		return "", errNotInstalled
	}
}

// fixtureDocker answers the Engine API like a host running a compose project.
// The listing and inspect answers carry environments, commands and labels
// full of sentinels, the way the real API does.
func fixtureDocker() func(ctx context.Context, path string) ([]byte, error) {
	return func(_ context.Context, path string) ([]byte, error) {
		switch {
		case path == "/containers/json?all=1":
			return []byte(`[{"Id":"abc123","Names":["/app-web-1"],"Image":"nginx:1.27","ImageID":"sha256:1111",` +
				`"Command":"/docker-entrypoint.sh --secret ` + sentinel("ctrcmd") + `","State":"running","Status":"Up 3 days",` +
				`"Ports":[{"IP":"0.0.0.0","PrivatePort":80,"PublicPort":8080,"Type":"tcp"}],` +
				`"Labels":{"com.docker.compose.project":"app","com.docker.compose.service":"web",` +
				`"com.docker.compose.project.working_dir":"/opt/app","com.docker.compose.project.config_files":"/opt/app/docker-compose.yml",` +
				`"traefik.http.middlewares.auth.basicauth.users":"admin:` + sentinel("label") + `"},` +
				`"Mounts":[{"Type":"bind","Source":"/opt/app/html","Destination":"/usr/share/nginx/html","RW":false},` +
				`{"Type":"volume","Name":"app_data","Source":"/var/lib/docker/volumes/app_data/_data","Destination":"/data","RW":true}]},` +
				`{"Id":"def456","Names":["/app-db-1"],"Image":"postgres:16","ImageID":"sha256:2222","State":"running",` +
				`"Ports":[],"Labels":{"com.docker.compose.project":"app","com.docker.compose.service":"db"},"Mounts":[]}]`), nil
		case path == "/images/json":
			return []byte(`[{"Id":"sha256:1111","RepoTags":["nginx:1.27"],"RepoDigests":["nginx@sha256:aaaa"]},{"Id":"sha256:2222","RepoDigests":[]}]`), nil
		case strings.HasPrefix(path, "/containers/abc123/json"):
			return []byte(`{"Id":"abc123","RestartCount":2,"HostConfig":{"RestartPolicy":{"Name":"unless-stopped"}},` +
				`"Config":{"Env":["PATH=/usr/bin","API_TOKEN=` + sentinel("composeenv") + `"],"Cmd":["--secret","` + sentinel("ctrcmd") + `"]}}`), nil
		case strings.HasPrefix(path, "/containers/def456/json"):
			return []byte(`{"Id":"def456","RestartCount":0,"HostConfig":{"RestartPolicy":{"Name":"always"}},` +
				`"Config":{"Env":["POSTGRES_PASSWORD=` + sentinel("composeenv") + `"]}}`), nil
		}
		return nil, fmt.Errorf("unexpected docker path %s", path)
	}
}

func fixtureEnv(t *testing.T, root string) describeEnv {
	t.Helper()
	return describeEnv{
		root:   root,
		run:    fixtureCommands(nil),
		docker: fixtureDocker(),
		euid:   999,
		now:    func() time.Time { return fixtureNow },
	}
}

func describeJSON(t *testing.T, env describeEnv) (map[string]any, string) {
	t.Helper()
	doc := describeHost(context.Background(), env)
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	var back map[string]any
	if err := json.Unmarshal(raw, &back); err != nil {
		t.Fatal(err)
	}
	return back, string(raw)
}

func section(t *testing.T, doc map[string]any, path ...string) map[string]any {
	t.Helper()
	cur := doc
	for _, p := range path {
		next, ok := cur[p].(map[string]any)
		if !ok {
			t.Fatalf("no section %v in the description", path)
		}
		cur = next
	}
	return cur
}

func list(m map[string]any, key string) []any {
	l, _ := m[key].([]any)
	return l
}

func TestDescriptionNeverCarriesASecret(t *testing.T) {
	_, raw := describeJSON(t, fixtureEnv(t, buildFixtureHost(t)))
	for _, name := range sentinelNames {
		if strings.Contains(raw, sentinel(name)) {
			t.Errorf("the %s sentinel left the host in the description", name)
		}
	}
	for _, marker := range []string{"PRIVATE KEY", "Bearer ", "DB_PASSWORD", "API_TOKEN", "POSTGRES_PASSWORD", "--token", "argv[]"} {
		if strings.Contains(raw, marker) {
			t.Errorf("the description carries %q", marker)
		}
	}
}

// #853: the units collector asks systemd for the program path and state only -
// never Environment, EnvironmentFiles or the drop-ins that carry them - even
// though a real `systemctl show` would answer those properties to anyone. The
// fixture's show answer carries them regardless (TestDescriptionNeverCarriesASecret
// proves applyShow drops them); this pins what is ASKED.
func TestTheUnitsCollectorNeverAsksForAnEnvironment(t *testing.T) {
	var asked []string
	env := fixtureEnv(t, buildFixtureHost(t))
	inner := env.run
	env.run = func(ctx context.Context, name string, args ...string) (string, error) {
		if name == "systemctl" {
			asked = append(asked, strings.Join(args, " "))
		}
		return inner(ctx, name, args...)
	}
	sec := collectUnits(context.Background(), env)
	if sec.Status != statusOK || len(sec.Units) == 0 {
		t.Fatalf("the units collector did not run: %+v", sec)
	}
	shows := 0
	for _, a := range asked {
		if strings.HasPrefix(a, "cat") {
			t.Errorf("the units collector prints unit files: systemctl %s", a)
		}
		if !strings.HasPrefix(a, "show") {
			continue
		}
		shows++
		// show with no -p (or --all) prints every property, Environment included.
		if !strings.Contains(a, " -p ") {
			t.Errorf("the units collector runs show without a property list: systemctl %s", a)
		}
		for _, bad := range []string{"Environment", "DropIn", "--all"} {
			if strings.Contains(a, bad) {
				t.Errorf("the units collector asks systemctl for %q: systemctl %s", bad, a)
			}
		}
	}
	if shows == 0 {
		t.Fatal("the units collector never ran systemctl show; the check is vacuous")
	}
}

// Teeth for the deny filter: the RAW collection of this fixture does carry
// sentinels (a credential in a proxy_pass URL, a bearer token in a return
// body) - only finishDescription stands between them and the hub. Remove the
// scrub from finishDescription and TestDescriptionNeverCarriesASecret fails.
func TestTheRawCollectionReachesTheDenyFilter(t *testing.T) {
	env := fixtureEnv(t, buildFixtureHost(t))
	rawTree := collectDescription(context.Background(), env)
	raw, _ := json.Marshal(rawTree)
	for _, name := range []string{"proxyuser", "returnbearer"} {
		if !strings.Contains(string(raw), sentinel(name)) {
			t.Fatalf("the fixture no longer exercises the deny filter: the raw collection lacks the %s sentinel", name)
		}
	}
	done := finishDescription(rawTree, 0)
	out, _ := json.Marshal(done)
	for _, name := range []string{"proxyuser", "returnbearer"} {
		if strings.Contains(string(out), sentinel(name)) {
			t.Errorf("the deny filter let the %s sentinel through", name)
		}
	}
	if done["redactions"].(int) < 2 {
		t.Errorf("redactions = %v, want at least the two the fixture plants", done["redactions"])
	}
}

func TestDescriptionDescribesTheReverseProxy(t *testing.T) {
	doc, _ := describeJSON(t, fixtureEnv(t, buildFixtureHost(t)))
	if doc["schema"] != describeSchema || doc["collected_at"] != "2026-11-18T12:00:00Z" {
		t.Fatalf("identity fields: %v %v", doc["schema"], doc["collected_at"])
	}
	host := section(t, doc, "host")
	if host["hostname"] != "proxy-1" || host["os"] != "Ubuntu 24.04.1 LTS" || host["kernel"] != "6.8.0-48-generic" {
		t.Errorf("host = %v", host)
	}

	// Sockets: the owned ones name nginx and its unit; the unowned one says so.
	sockets := section(t, doc, "listening")
	if sockets["status"] != statusPartial {
		t.Errorf("listening status = %v, want partial (53/udp's owner is not visible)", sockets["status"])
	}
	got := map[string]map[string]any{}
	for _, s := range list(sockets, "sockets") {
		m := s.(map[string]any)
		got[fmt.Sprintf("%s/%v/%s", m["proto"], m["port"], m["address"])] = m
	}
	if len(got) != 4 {
		t.Errorf("sockets = %v, want 443, [::]:80, 127.0.0.1:5432, 53/udp (no established ones)", got)
	}
	if s := got["tcp/443/0.0.0.0"]; s == nil || s["process"] != "nginx" || s["unit"] != "nginx.service" || s["user"] != "root" {
		t.Errorf("443 = %v", s)
	}
	if s := got["tcp/80/::"]; s == nil || s["process"] != "nginx" {
		t.Errorf("[::]:80 = %v", s)
	}
	if s := got["udp/53/127.0.0.53"]; s == nil || s["owner"] != "not visible to the agent" || s["process"] != nil {
		t.Errorf("53/udp = %v", s)
	}
	if s := got["tcp/5432/127.0.0.1"]; s == nil || s["user"] != "hp-agent" {
		t.Errorf("5432 = %v", s)
	}

	// Units: active or enabled, program path only, custom flagged.
	units := map[string]map[string]any{}
	for _, u := range list(section(t, doc, "units"), "units") {
		m := u.(map[string]any)
		units[m["name"].(string)] = m
	}
	for _, absent := range []string{"apt-daily.service", "ghost.service", "getty@.service"} {
		if units[absent] != nil {
			t.Errorf("%s is neither active nor enabled (or not a unit), yet reported", absent)
		}
	}
	if u := units["nginx.service"]; u == nil || u["active"] != "active" || u["enabled"] != "enabled" ||
		fmt.Sprint(u["programs"]) != "[/usr/sbin/nginx]" || u["custom"] != nil {
		t.Errorf("nginx.service = %v", u)
	}
	if u := units["app.service"]; u == nil || fmt.Sprint(u["programs"]) != "[/opt/app/bin/app]" || u["custom"] != true {
		t.Errorf("app.service = %v", u)
	}
	if u := units["backup.service"]; u == nil || fmt.Sprint(u["programs"]) != "[/usr/local/bin/backup /usr/bin/true]" || u["active"] != nil {
		t.Errorf("backup.service (enabled, not loaded) = %v", u)
	}
	if u := units["certbot.timer"]; u == nil || u["type"] != "timer" {
		t.Errorf("certbot.timer = %v", u)
	}

	// Docker: containers and the compose project, digests, restart counts.
	dock := section(t, doc, "docker")
	ctrs := list(dock, "containers")
	if dock["status"] != statusOK || len(ctrs) != 2 {
		t.Fatalf("docker = %v", dock)
	}
	web := ctrs[1].(map[string]any)
	if web["name"] != "app-web-1" || web["image"] != "nginx:1.27" || web["digest"] != "sha256:aaaa" ||
		web["restart_count"] != float64(2) || web["restart_policy"] != "unless-stopped" ||
		web["compose_project"] != "app" || web["compose_service"] != "web" {
		t.Errorf("web container = %v", web)
	}
	if fmt.Sprint(web["ports"]) != "[map[container_port:80 host_port:8080 ip:0.0.0.0 proto:tcp]]" {
		t.Errorf("ports = %v", web["ports"])
	}
	if fmt.Sprint(web["mounts"]) != "[map[destination:/usr/share/nginx/html rw:false source:/opt/app/html type:bind] map[destination:/data rw:true type:volume volume:app_data]]" {
		t.Errorf("mounts = %v", web["mounts"])
	}
	for _, k := range []string{"env", "Env", "command", "Command", "labels", "Labels", "Config"} {
		if _, has := web[k]; has {
			t.Errorf("container carries %q", k)
		}
	}
	compose := list(dock, "compose_projects")
	if fmt.Sprint(compose) != "[map[config_files:[/opt/app/docker-compose.yml] name:app services:[db web] working_dir:/opt/app]]" {
		t.Errorf("compose = %v", compose)
	}

	// nginx: servers, upstream, include graph, TLS, headers.
	ng := section(t, doc, "nginx")
	if ng["status"] != statusOK {
		t.Errorf("nginx = %v", ng["status"])
	}
	files := map[string]string{}
	for _, f := range list(ng, "files") {
		m := f.(map[string]any)
		files[m["file"].(string)] = fmt.Sprint(m["includes"])
	}
	if files["/etc/nginx/nginx.conf"] != "[/etc/nginx/mime.types /etc/nginx/conf.d/upstreams.conf /etc/nginx/sites-enabled/estate.conf]" {
		t.Errorf("include graph = %v", files)
	}
	servers := list(ng, "servers")
	if len(servers) != 3 {
		t.Fatalf("servers = %d, want 2 http + 1 stream", len(servers))
	}
	redirect := servers[0].(map[string]any)
	if fmt.Sprint(redirect["server_names"]) != "[example.com www.example.com]" || fmt.Sprint(redirect["listen"]) != "[80 [::]:80]" ||
		redirect["return"] != "301 https://$host$request_uri" || redirect["file"] != "/etc/nginx/sites-enabled/estate.conf" {
		t.Errorf("redirect server = %v", redirect)
	}
	tlsServer := servers[1].(map[string]any)
	tls := tlsServer["tls"].(map[string]any)
	if tls["certificate"] != "/etc/ssl/certs/example.pem" || tls["certificate_key"] != "present" ||
		tls["client_certificate"] != "/etc/nginx/client-ca.pem" || tls["verify_client"] != "optional" {
		t.Errorf("tls = %v", tls)
	}
	if tlsServer["root"] != "/var/www/example" || fmt.Sprint(tlsServer["listen"]) != "[443 ssl http2]" {
		t.Errorf("tls server = %v", tlsServer)
	}
	headers := fmt.Sprint(tlsServer["headers"])
	if headers != "[map[name:Host value:$host] map[name:X-Forwarded-For value:$proxy_add_x_forwarded_for] map[name:Authorization value_dropped:true]]" {
		t.Errorf("server headers = %s", headers)
	}
	locs := map[string]map[string]any{}
	for _, l := range list(tlsServer, "locations") {
		m := l.(map[string]any)
		locs[m["match"].(string)] = m
	}
	if locs["/"]["proxy_pass"] != "http://app_backend" || locs["/static/"]["alias"] != "/srv/static/" {
		t.Errorf("locations = %v", locs)
	}
	if fmt.Sprint(locs["/"]["headers"]) != "[map[name:Cookie value_dropped:true] map[name:X-Api-Key value_dropped:true]]" {
		t.Errorf("location headers = %v", locs["/"]["headers"])
	}
	if locs["/legacy"]["proxy_pass"] != "https://[redacted]@10.0.0.30:8443" {
		t.Errorf("userinfo must go, the target must stay: %v", locs["/legacy"]["proxy_pass"])
	}
	if locs["= /probe"]["return"] != redactedMark {
		t.Errorf("a bearer token in a return body: %v", locs["= /probe"]["return"])
	}
	stream := servers[2].(map[string]any)
	if stream["context"] != "stream" || stream["proxy_pass"] != "10.0.0.20:5432" {
		t.Errorf("stream server = %v", stream)
	}
	if fmt.Sprint(list(ng, "upstreams")) != "[map[file:/etc/nginx/conf.d/upstreams.conf name:app_backend servers:[10.0.0.10:8080 10.0.0.11:8080]]]" {
		t.Errorf("upstreams = %v", list(ng, "upstreams"))
	}

	// Certificates: nginx's and certbot's, with expiry; certbot's live dir is
	// absent from the fixture, so that one reports it cannot be read.
	certs := section(t, doc, "certificates")
	byPath := map[string]map[string]any{}
	for _, c := range list(certs, "certificates") {
		m := c.(map[string]any)
		byPath[m["path"].(string)] = m
	}
	site := byPath["/etc/ssl/certs/example.pem"]
	if site == nil || site["subject"] != "example.com" || site["days_left"] != float64(30) || site["source"] != "nginx" ||
		fmt.Sprint(site["names"]) != "[example.com www.example.com]" || site["not_after"] != "2026-12-18T12:00:00Z" {
		t.Errorf("site certificate = %v", site)
	}
	if ca := byPath["/etc/nginx/client-ca.pem"]; ca == nil || ca["subject"] != "Estate Client CA" {
		t.Errorf("client CA = %v", ca)
	}
	if le := byPath["/etc/letsencrypt/live/example.org/cert.pem"]; le == nil || le["status"] != statusNotInstalled || le["source"] != "letsencrypt" {
		t.Errorf("certbot certificate = %v", le)
	}
	if fmt.Sprint(list(certs, "letsencrypt")) != "[map[authenticator:webroot certificate:/etc/letsencrypt/live/example.org/cert.pem domains:[example.org www.example.org] installer:nginx name:example.org]]" {
		t.Errorf("letsencrypt = %v", list(certs, "letsencrypt"))
	}

	// Firewall, updates, time.
	ufw := section(t, doc, "firewall", "ufw")
	if ufw["enabled"] != true || len(list(ufw, "rules")) != 3 || ufw["rules_status"] != statusOK {
		t.Errorf("ufw = %v", ufw)
	}
	if fmt.Sprint(list(ufw, "rules")[0]) != "map[action:allow direction:in from:10.0.0.0/8 port:22 proto:tcp]" {
		t.Errorf("ufw rule = %v", list(ufw, "rules")[0])
	}
	nft := section(t, doc, "firewall", "nftables")
	if fmt.Sprint(list(nft, "tables")) != "[map[chains:[map[hook:forward name:forward policy:accept rules:0] map[hook:input name:input policy:drop rules:2]] family:inet name:filter]]" {
		t.Errorf("nftables = %v", nft)
	}
	upd := section(t, doc, "updates")
	if upd["pending"] != float64(7) || upd["security"] != float64(3) || upd["reboot_required"] != true {
		t.Errorf("updates = %v", upd)
	}
	tm := section(t, doc, "time")
	if tm["synchronized"] != true || tm["ntp_enabled"] != true || tm["timezone"] != "America/Chicago" {
		t.Errorf("time = %v", tm)
	}
	if doc["truncated"] != nil {
		t.Errorf("a small host was truncated: %v", doc["truncated"])
	}
}

// An unprivileged agent: what its user cannot read is "not visible", per part,
// and the rest of the description is still there.
func TestWhatTheAgentCannotReadIsNotVisibleNotAFailure(t *testing.T) {
	root := buildFixtureHost(t)
	env := fixtureEnv(t, root)
	env.run = fixtureCommands(map[string]func() (string, error){
		"nft ":         func() (string, error) { return "", &cmdError{code: 1, stderr: "Error: Operation not permitted"} },
		"timedatectl ": func() (string, error) { return "", &cmdError{code: 1, stderr: "Failed to query server: Access denied"} },
	})
	env.docker = func(context.Context, string) ([]byte, error) { return nil, os.ErrPermission }
	doc, _ := describeJSON(t, env)
	if s := section(t, doc, "firewall", "nftables"); s["status"] != statusNotVisible || s["reason"] != notVisibleReason {
		t.Errorf("nftables = %v", s)
	}
	if s := section(t, doc, "time"); s["status"] != statusNotVisible {
		t.Errorf("time = %v", s)
	}
	d := section(t, doc, "docker")
	if d["status"] != statusNotVisible || !strings.HasPrefix(d["reason"].(string), "docker: not readable") {
		t.Errorf("docker = %v", d)
	}
	if section(t, doc, "nginx")["status"] != statusOK || section(t, doc, "units")["status"] != statusOK {
		t.Error("one unreadable part took the others down")
	}

	if os.Geteuid() == 0 {
		t.Skip("file-permission checks need a non-root test user")
	}
	for _, p := range []string{"etc/ufw/user.rules", "etc/ufw/user6.rules", "etc/ssl/certs/example.pem", "etc/nginx/conf.d/upstreams.conf"} {
		if err := os.Chmod(filepath.Join(root, p), 0); err != nil {
			t.Fatal(err)
		}
	}
	doc, _ = describeJSON(t, env)
	ufw := section(t, doc, "firewall", "ufw")
	if ufw["status"] != statusPartial || ufw["rules_status"] != statusNotVisible || ufw["enabled"] != true {
		t.Errorf("ufw with unreadable rules = %v", ufw)
	}
	if ng := section(t, doc, "nginx"); ng["status"] != statusPartial || len(list(ng, "servers")) != 3 {
		t.Errorf("nginx with an unreadable include = %v", ng["status"])
	}
	certs := section(t, doc, "certificates")
	if certs["status"] != statusPartial {
		t.Errorf("certificates = %v", certs["status"])
	}
}

// A bare host: every part says what is missing; the document is still whole.
func TestABareHostIsDescribedAsBare(t *testing.T) {
	env := describeEnv{
		root:   t.TempDir(),
		run:    func(context.Context, string, ...string) (string, error) { return "", errNotInstalled },
		docker: func(context.Context, string) ([]byte, error) { return nil, errDockerNoSocket },
		now:    func() time.Time { return fixtureNow },
	}
	doc, _ := describeJSON(t, env)
	for _, s := range [][]string{{"nginx"}, {"docker"}, {"units"}, {"time"}, {"firewall", "ufw"}, {"firewall", "nftables"}, {"updates"}} {
		if got := section(t, doc, s...)["status"]; got != statusNotInstalled {
			t.Errorf("%v status = %v, want not_installed", s, got)
		}
	}
	if section(t, doc, "listening")["status"] == statusOK {
		t.Error("no /proc/net, yet sockets reported ok")
	}
}

// The caps: every string, and the whole document, whatever the host holds.
func TestTheDescriptionAlwaysFitsOneFrame(t *testing.T) {
	root := buildFixtureHost(t)
	var conf strings.Builder
	conf.WriteString("http {\n")
	for i := 0; i < 290; i++ {
		fmt.Fprintf(&conf, "server { listen %d; server_name %s; location / { proxy_pass http://10.0.0.1:%d; } }\n",
			10000+i, strings.Repeat("n", 2000)+fmt.Sprint(i), i)
	}
	conf.WriteString("}\n")
	writeFixture(t, root, "etc/nginx/nginx.conf", conf.String())
	env := fixtureEnv(t, root)
	var units strings.Builder
	for i := 0; i < 3000; i++ {
		fmt.Fprintf(&units, "unit-%04d-%s.service loaded active running x\n", i, strings.Repeat("u", 150))
	}
	env.run = fixtureCommands(map[string]func() (string, error){
		"systemctl list-units": func() (string, error) { return units.String(), nil },
	})
	doc := describeHost(context.Background(), env)
	raw, _ := json.Marshal(doc)
	if len(raw) > describeDocCap {
		t.Fatalf("document is %d bytes, cap %d", len(raw), describeDocCap)
	}
	if doc["truncated"] == nil {
		t.Error("a trimmed document must say what was cut")
	}
	frame, err := encodeMessage(msg{"action": "command_result", "for_action": "describe_host", "description": doc, "request_id": "r"})
	if err != nil || len(frame) > maxMessageSize {
		t.Fatalf("the frame does not fit: %v", err)
	}
	for _, s := range list(section(t, doc, "nginx"), "servers") {
		for _, n := range list(s.(map[string]any), "server_names") {
			if len(n.(string)) > describeFieldCap+len("...[truncated]") {
				t.Fatalf("a field of %d bytes passed the cap", len(n.(string)))
			}
		}
	}
	if len(list(section(t, doc, "units"), "units")) > maxUnits {
		t.Error("more units than maxUnits")
	}
}

func TestDenyFilterValues(t *testing.T) {
	kept := []string{
		"nginx:1.27", "sha256:" + strings.Repeat("ab", 32), "/etc/nginx/sites-enabled/estate.conf",
		"http://app_backend", "301 https://$host$request_uri", "$proxy_add_x_forwarded_for",
		"unless-stopped", "10.0.0.0/8", "/usr/sbin/nginx", "Run certbot twice daily",
	}
	for _, v := range kept {
		s := &scrubber{}
		if got := s.str(v); got != v || s.redacted != 0 {
			t.Errorf("%q was changed to %q", v, got)
		}
	}
	// Assembled from fragments: no credential-shaped literal in the tree.
	redacted := []string{
		"Bearer " + "abcdef" + "0123456789",
		"basic " + "dXNlcjpw" + "YXNzd29yZA==",
		"password" + "=hunter2hunter2",
		"API_" + "KEY: abcd",
		"client_" + "secret=xyz",
		"gh" + "p_" + strings.Repeat("A", 36),
		"AK" + "IA" + "ABCDEFGHIJKLMNOP",
		"eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".abcdefghijkl",
		"-----BEGIN " + "RSA PRIVATE KEY-----",
		"hp" + "_0123456789abc",
	}
	for _, v := range redacted {
		s := &scrubber{}
		if got := s.str(v); got != redactedMark || s.redacted == 0 {
			t.Errorf("%q passed the deny filter as %q", v, got)
		}
	}
	s := &scrubber{}
	if got := s.str("postgres://app:" + "pw" + "@db:5432/app"); got != "postgres://[redacted]@db:5432/app" {
		t.Errorf("userinfo: %q", got)
	}
	tree := s.walk(map[string]any{"name": "X-Auth-Token", "value": "plain"}).(map[string]any)
	if tree["value"] != redactedMark {
		t.Errorf("a credential-named pair kept its value: %v", tree)
	}
}

func TestProcNetAddresses(t *testing.T) {
	for in, want := range map[string]string{
		"0100007F:0035":                         "127.0.0.1:53",
		"00000000:01BB":                         "0.0.0.0:443",
		"00000000000000000000000001000000:0016": "::1:22",
		"0000000000000000FFFF00000100007F:1F90": "127.0.0.1:8080", // v4-mapped prints as v4
	} {
		addr, port, ok := parseHexAddr(in)
		if !ok || fmt.Sprintf("%s:%d", addr, port) != want {
			t.Errorf("%s -> %s:%d, want %s", in, addr, port, want)
		}
	}
}

func TestNginxTokenizer(t *testing.T) {
	toks := tokenizeNginx("a 'b c' \"d;e\" f; # comment {\n g { h; }")
	if fmt.Sprint(toks) != "[a b c d;e f ; g { h ; }]" {
		t.Errorf("tokens = %q", toks)
	}
	nodes, _ := parseNginx(toks, "/x")
	if len(nodes) != 2 || fmt.Sprint(nodes[0].args) != "[b c d;e f]" || !nodes[1].block || nodes[1].children[0].name != "h" {
		t.Errorf("tree = %+v", nodes)
	}
}

// The real command runner: finds the command under root's bin directories,
// runs it with no inherited environment, and classifies a refusal.
func TestTheCommandRunnerUsesTheRootsBinaries(t *testing.T) {
	root := t.TempDir()
	writeFixture(t, root, "usr/bin/timedatectl", "#!/bin/sh\necho \"NTPSynchronized=yes\"\necho \"leak=$HP_AGENT_AUTH_TOKEN\"\n")
	writeFixture(t, root, "usr/sbin/nft", "#!/bin/sh\necho 'Error: Operation not permitted' >&2\nexit 1\n")
	for _, p := range []string{"usr/bin/timedatectl", "usr/sbin/nft"} {
		if err := os.Chmod(filepath.Join(root, p), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("HP_AGENT_AUTH_TOKEN", "inherited")
	run := describeCommandRunner(root)
	out, err := run(context.Background(), "timedatectl", "show")
	if err != nil || !strings.Contains(out, "NTPSynchronized=yes") || strings.Contains(out, "inherited") {
		t.Errorf("timedatectl: %q %v (the agent's environment must not reach a command)", out, err)
	}
	if _, err := run(context.Background(), "nft", "-j", "list", "ruleset"); err == nil {
		t.Error("a failing command reported success")
	} else if status, _ := classifyErr(err); status != statusNotVisible {
		t.Errorf("nft refusal classified %s", status)
	}
	if _, err := run(context.Background(), "ufw", "status"); !errors.Is(err, errNotInstalled) {
		t.Errorf("a missing command: %v", err)
	}
}

// The real Docker client over a unix socket under root.
func TestTheDockerClientTalksToTheSocketUnderRoot(t *testing.T) {
	root, err := os.MkdirTemp("", "hpd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	if err := os.MkdirAll(filepath.Join(root, "run"), 0o755); err != nil {
		t.Fatal(err)
	}
	sock := filepath.Join(root, "run", "docker.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Skipf("no unix socket here: %v", err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/containers/json" {
			_, _ = w.Write([]byte(`[]`))
			return
		}
		http.NotFound(w, r)
	})}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	client := dockerClient(root)
	if body, err := client(context.Background(), "/containers/json?all=1"); err != nil || string(body) != "[]" {
		t.Fatalf("docker API: %q %v", body, err)
	}
	if _, err := dockerClient(t.TempDir())(context.Background(), "/containers/json"); !errors.Is(err, errDockerNoSocket) {
		t.Errorf("no socket: %v", err)
	}
	if os.Geteuid() != 0 {
		if err := os.Chmod(sock, 0); err != nil {
			t.Fatal(err)
		}
		if _, err := client(context.Background(), "/containers/json"); !errors.Is(err, os.ErrPermission) {
			t.Errorf("an unreadable socket: %v, want a permission error", err)
		}
	}
}

func TestTheAgentAdvertisesTheDescription(t *testing.T) {
	caps, _ := collectSystemInfo()["capabilities"].([]string)
	if !contains(caps, "describe_host") {
		t.Fatalf("capabilities = %v", caps)
	}
	if _, ok := workerActions["describe_host"]; !ok {
		t.Fatal("describe_host is advertised but not served")
	}
}
