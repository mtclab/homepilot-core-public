package main

import (
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #853: unit files and drop-ins are readable at the read tier, and they hold
// other services' secrets - `Environment=TOKEN=...` in a drop-in, a credential
// set inline, the files a unit loads its environment from.
//
// The journey: a fixture host (HP_AGENT_READ_PREFIXES + HP_AGENT_UNIT_DIRS
// point the real read gate at it) holds a service, its drop-in, a WireGuard
// netdev and the environment/credential files they name, each carrying a
// sentinel. EVERY file under it is read through every read-tier path the agent
// has - read_file (what the hub's read_file action calls) and `cat` through the
// real allowlist and Executor, unprivileged and privileged - and no answer may
// contain a sentinel. The files on disk DO contain them (checked first), and
// the unit files are still served with their variable names, so the test
// cannot pass by refusing everything.
//
// Teeth: make readFile return the content unredacted, drop the exec_keys case
// from redactUnitText (the drop-in's ExecStart --token), or drop the
// unitSecretSourceRefusal call from checkReadPath, and
// TestNoReadTierPathShowsAUnitsSecrets fails (verified when #853 was built).

// unitSentinel is assembled so no credential-shaped literal sits in the tree.
func unitSentinel(name string) string { return "hpSENTINEL" + "853" + name + "zq" }

type unitCorpus struct {
	UnitRedaction struct {
		Shaped    []string            `json:"shaped"`
		NotShaped []string            `json:"not_shaped"`
		Cases     [][]json.RawMessage `json:"cases"`
	} `json:"unit_redaction"`
}

func loadUnitCorpus(t *testing.T) unitCorpus {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "readonly_policy_corpus.json"))
	if err != nil {
		t.Fatal(err)
	}
	var c unitCorpus
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	if len(c.UnitRedaction.Cases) < 10 || len(c.UnitRedaction.Shaped) == 0 {
		t.Fatal("unit_redaction corpus parsed empty")
	}
	return c
}

// The agent's rewrite produces exactly the corpus's bytes - the same corpus
// the control plane's mirror is held to - and is a fixed point.
func TestUnitRedactionCorpus(t *testing.T) {
	c := loadUnitCorpus(t)
	for _, tc := range c.UnitRedaction.Cases {
		var name, in, want string
		var count int
		for i, dst := range []any{&name, &in, &want, &count} {
			if err := json.Unmarshal(tc[i], dst); err != nil {
				t.Fatal(err)
			}
		}
		got, n := redactUnitText(in)
		if got != want || n != count {
			t.Errorf("%s:\n in   %q\n got  %q (%d)\n want %q (%d)", name, in, got, n, want, count)
		}
		if again, _ := redactUnitText(got); again != got {
			t.Errorf("%s: the rewrite is not a fixed point:\n %q\n %q", name, got, again)
		}
	}
	for _, p := range c.UnitRedaction.Shaped {
		if !isUnitShaped(p) {
			t.Errorf("%s is not treated as a unit file", p)
		}
	}
	for _, p := range c.UnitRedaction.NotShaped {
		if isUnitShaped(p) {
			t.Errorf("%s is treated as a unit file", p)
		}
	}
}

// buildUnitFixture lays out a host's units under root and returns the paths a
// read must refuse (the secret sources) and the unit files it must serve.
func buildUnitFixture(t *testing.T, root string) (sources, units []string) {
	t.Helper()
	write := func(rel, content string) string {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	conf := filepath.Join(root, "conf")
	sources = []string{
		// Named plainly: nothing in these names says env, secret or token, so
		// the name-based deny rules cannot be what refuses them.
		write("conf/app-settings", "API_KEY="+unitSentinel("envfile")+"\n"),
		write("conf/instance-web", "DB_URL="+unitSentinel("specifier")+"\n"),
		write("conf/dbpass.bin", unitSentinel("loadcred")+"\n"),
		write("conf/creddir/one", unitSentinel("creddir")+"\n"),
		write("conf/peer-psk", unitSentinel("pskfile")+"\n"),
		write("conf/commented-old", "OLD="+unitSentinel("commentedfile")+"\n"),
	}
	link := filepath.Join(conf, "settings-link")
	if err := os.Symlink(filepath.Join(conf, "app-settings"), link); err != nil {
		t.Fatal(err)
	}
	sources = append(sources, link)
	write("conf/readme.txt", "plain notes, no secret here\n")

	units = []string{
		write("units/app.service", "[Unit]\nDescription=app\n[Service]\n"+
			"Environment=TOKEN="+unitSentinel("env")+"\n"+
			"Environment=\"A="+unitSentinel("quoted")+"\" \"B=two "+unitSentinel("quoted2")+"\"\n"+
			"Environment=C=1 \\\n  D="+unitSentinel("continued")+"\n"+
			"EnvironmentFile="+conf+"/app-settings\n"+
			"EnvironmentFile=-"+conf+"/instance-%i\n"+
			"LoadCredential=db:"+conf+"/dbpass.bin\n"+
			"LoadCredential=dir:"+conf+"/creddir\n"+
			"SetCredential=api:"+unitSentinel("setcred")+"\n"+
			"#Environment=OLD="+unitSentinel("commented")+"\n"+
			"#EnvironmentFile="+conf+"/commented-old\n"+
			"ExecStart=/usr/bin/app\n"),
		write("units/app.service.d/override.conf", "[Service]\n"+
			"Environment=\"PASSWORD="+unitSentinel("dropin")+"\"\n"+
			"environment = LOWER="+unitSentinel("lower")+"\n"+
			// The #817 class in a unit file: argv carries a token.
			"ExecStart=\nExecStart=/usr/bin/app --token="+unitSentinel("execarg")+"\n"+
			"ExecStartPre=-/usr/bin/prep \\\n  --key "+unitSentinel("execcont")+"\n"),
		write("units/wg0.netdev", "[NetDev]\nName=wg0\nKind=wireguard\n[WireGuard]\n"+
			"PrivateKey="+unitSentinel("wgkey")+"\n[WireGuardPeer]\n"+
			"PresharedKeyFile="+conf+"/peer-psk\n"),
		write("units/system.conf", "[Manager]\nDefaultEnvironment=PROXY="+unitSentinel("default")+"\n"),
	}
	return sources, units
}

func TestNoReadTierPathShowsAUnitsSecrets(t *testing.T) {
	defaultReadEnv(t)
	root := t.TempDir()
	sources, units := buildUnitFixture(t, root)
	t.Setenv("HP_AGENT_READ_PREFIXES", root)
	t.Setenv("HP_AGENT_UNIT_DIRS", filepath.Join(root, "units"))

	var all []string
	sentinels := 0
	if err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			all = append(all, p)
			raw, _ := os.ReadFile(p)
			sentinels += strings.Count(string(raw), "hpSENTINEL853")
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	// Control: the secrets are really on disk, so a clean answer is a result.
	if sentinels < 15 {
		t.Fatalf("the fixture holds only %d sentinels; it is not the host the test thinks", sentinels)
	}

	unpriv := Executor{allow: Allowlist{privileged: false}}
	priv := Executor{allow: Allowlist{privileged: true}}
	for _, p := range all {
		content, err := readFile(p)
		if strings.Contains(content, "hpSENTINEL853") || (err != nil && strings.Contains(err.Error(), "hpSENTINEL853")) {
			t.Errorf("read_file %s returned a unit's secret:\n%s", p, content)
		}
		for mode, ex := range map[string]Executor{"unprivileged": unpriv, "privileged": priv} {
			code, stdout, stderr := ex.Exec("cat "+p, 10)
			if strings.Contains(stdout+stderr, "hpSENTINEL853") {
				t.Errorf("%s `cat %s` returned a unit's secret (exit %d):\n%s", mode, p, code, stdout)
			}
		}
	}

	// The secret sources are refused, by name of the unit that loads them.
	for _, p := range sources {
		if _, err := readFile(p); err == nil || !strings.Contains(err.Error(), "secret source") {
			t.Errorf("read_file %s: want a secret-source refusal, got %v", p, err)
		}
		if ok, reason := unpriv.allow.IsAllowed("cat " + p); ok || !strings.Contains(reason, "secret source") {
			t.Errorf("cat %s: want a secret-source refusal, got ok=%v %s", p, ok, reason)
		}
	}
	// The units are SERVED, names intact: the read tier still explains them.
	for _, p := range units {
		content, err := readFile(p)
		if err != nil {
			t.Errorf("read_file %s refused: %v", p, err)
			continue
		}
		if !strings.Contains(content, "<redacted>") {
			t.Errorf("read_file %s carries no redaction mark:\n%s", p, content)
		}
	}
	dropin, _ := readFile(units[1])
	for _, want := range []string{"ExecStart=\n", "ExecStart=/usr/bin/app <arguments redacted>",
		"ExecStartPre=-/usr/bin/prep <arguments redacted>"} {
		if !strings.Contains(dropin, want) {
			t.Errorf("the served drop-in lost %q:\n%s", want, dropin)
		}
	}
	app, _ := readFile(units[0])
	for _, want := range []string{"Environment=TOKEN=<redacted>", "Environment=\"A=<redacted>\" \"B=<redacted>\"",
		"Environment=C=<redacted> D=<redacted>", "SetCredential=api:<redacted>", "ExecStart=/usr/bin/app"} {
		if !strings.Contains(app, want) {
			t.Errorf("the served unit lost %q:\n%s", want, app)
		}
	}
	if plain, err := readFile(filepath.Join(root, "conf", "readme.txt")); err != nil || !strings.Contains(plain, "plain notes") {
		t.Errorf("a file no unit names is refused or changed: %q %v", plain, err)
	}
}

// A unit symlinked into a systemd directory from elsewhere (`systemctl link`)
// is still scanned, and a unit named like one outside any systemd directory is
// still redacted.
func TestALinkedUnitIsScannedAndRedacted(t *testing.T) {
	defaultReadEnv(t)
	root := t.TempDir()
	t.Setenv("HP_AGENT_READ_PREFIXES", root)
	t.Setenv("HP_AGENT_UNIT_DIRS", filepath.Join(root, "units"))
	if err := os.MkdirAll(filepath.Join(root, "units"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "opt"), 0o755); err != nil {
		t.Fatal(err)
	}
	env := filepath.Join(root, "opt", "runtime-values")
	if err := os.WriteFile(env, []byte("K="+unitSentinel("linked")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	unit := filepath.Join(root, "opt", "linked.service")
	if err := os.WriteFile(unit, []byte("[Service]\nEnvironmentFile="+env+"\nEnvironment=K="+unitSentinel("linkedenv")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(unit, filepath.Join(root, "units", "linked.service")); err != nil {
		t.Fatal(err)
	}
	if _, err := readFile(env); err == nil || !strings.Contains(err.Error(), "secret source") {
		t.Errorf("the env file a linked unit names is readable: %v", err)
	}
	for _, p := range []string{unit, filepath.Join(root, "units", "linked.service")} {
		content, err := readFile(p)
		if err != nil || strings.Contains(content, "hpSENTINEL853") || !strings.Contains(content, "K=<redacted>") {
			t.Errorf("read_file %s: %q %v", p, content, err)
		}
	}
}
