// Client-side guard for the agent install one-liner. The hub host is
// interpolated into a `curl … | bash` command run as root on a managed box, so
// a malformed/injected value must never be rendered or copied. Accepts only a
// bare IPv4 address or a DNS hostname (letters, digits, hyphens, dots) — no
// schemes, ports, whitespace, shell metacharacters, or paths. The server adds
// its own validation; this is defence in depth.
const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
const HOSTNAME =
	/^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$/;

export function isValidHubHost(host: string | null | undefined): boolean {
	if (!host) return false;
	if (/\s/.test(host)) return false;
	return IPV4.test(host) || HOSTNAME.test(host);
}
