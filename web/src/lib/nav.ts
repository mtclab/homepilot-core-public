// Same-origin guard for `returnTo` redirects. An attacker-supplied `returnTo`
// must never send the user off-origin (open redirect) and must never reach
// SvelteKit's `goto` as a full/off-app URL — `goto` throws on those, which made
// a SUCCESSFUL login look like it failed. Returns a safe in-app path, falling
// back when the input is missing, malformed, off-origin, or outside `base`.
export function safeReturnTo(
	raw: string | null | undefined,
	base: string,
	fallback: string,
): string {
	if (!raw) return fallback;

	let value = raw;
	try {
		value = decodeURIComponent(raw);
	} catch {
		return fallback;
	}

	// Must be an absolute in-app path, never a full URL (`http://…`),
	// protocol-relative (`//evil.com`), or backslash-tricked (`/\evil.com`).
	if (!value.startsWith('/')) return fallback;
	if (value.startsWith('//') || value.startsWith('/\\')) return fallback;

	// Must stay within the app's base path.
	if (value !== base && !value.startsWith(base + '/')) return fallback;

	return value;
}
