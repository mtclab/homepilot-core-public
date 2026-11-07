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
	label: string;
	cls: string;
}

const SUPERUSER = new Set(['*', 'all', 'full']);

export function scopeTier(
	capabilities: readonly string[] | null | undefined,
	scope: string | null | undefined,
): ScopeTier | null {
	const raw = (scope ?? '').trim();
	if (SUPERUSER.has(raw)) return { label: 'all', cls: 'text-ok' };
	const caps = capabilities ?? [];
	if (caps.includes('admin')) return { label: 'admin', cls: 'text-warn' };
	if (caps.includes('write')) return { label: 'read, write', cls: 'text-accent' };
	if (caps.includes('read')) return { label: 'read only', cls: 'text-muted' };
	return null;
}
