// The server hands the UI a normalized capability list on /auth/me
// (["read"], ["read","write"], ["read","write","admin"], …). The UI must gate
// write/admin controls off THIS list — never by re-deriving from the raw scope
// string, which previously mis-classified a plain `read,write` token as
// read-only. `admin` implies write server-side (normalize_scope), so we accept
// either for write-gated UI.

export function canWrite(capabilities: readonly string[] | null | undefined): boolean {
	const caps = capabilities ?? [];
	return caps.includes('write') || caps.includes('admin');
}

export function isAdmin(capabilities: readonly string[] | null | undefined): boolean {
	return (capabilities ?? []).includes('admin');
}
