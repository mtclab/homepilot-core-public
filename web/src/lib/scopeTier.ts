// The access tier the sidebar badge names (#704).
//
// It used to map the raw scope string against `*`/`full`/`read_only`, so the
// scopes tokens are actually minted with today - `all`, `admin`,
// `read,write`, `read` - all fell through to the muted "unknown" style, and a
// legacy spelling decided the colour. The tier comes from the server's
// normalized capability list (`/auth/me`), the same list the UI gates controls
// on; only "superuser" needs the scope itself, because `all` and `admin` both
// normalize to read+write+admin.

export interface ScopeTier {
	/** Said as words under the signed-in name, e.g. "Full access". */
	label: string;
}

const SUPERUSER = new Set(['*', 'all', 'full']);

export function scopeTier(
	capabilities: readonly string[] | null | undefined,
	scope: string | null | undefined,
): ScopeTier | null {
	const raw = (scope ?? '').trim();
	if (SUPERUSER.has(raw)) return { label: 'Full access' };
	const caps = capabilities ?? [];
	if (caps.includes('admin')) return { label: 'Admin access' };
	if (caps.includes('write')) return { label: 'Can read and change' };
	if (caps.includes('read')) return { label: 'Read only' };
	return null;
}
