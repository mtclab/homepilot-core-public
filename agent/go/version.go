package main

// version is the agent's build stamp. It is deliberately a package-level `var`
// in package `main` so the release build can set it:
//
//	go build -ldflags "-X main.version=v2.8.0"
//
// The release workflow HAD that ldflag long before this symbol existed, and Go
// silently ignores `-X` for a symbol it cannot find - so every released binary
// was unversioned while the pipeline reported success (#430). Nothing warns you
// about that; only a build that stamps a value and then reads it back does.
//
// "dev" is what an unstamped local build honestly is, and it is what the hub
// will show for a host built by hand rather than installed from a release.
var version = "dev"

// agentVersion is what the agent reports about itself: its build stamp, never
// empty. Callers (`--version`, the register payload) go through this so there is
// one answer to "which binary is this".
func agentVersion() string {
	if version == "" {
		return "dev"
	}
	return version
}
