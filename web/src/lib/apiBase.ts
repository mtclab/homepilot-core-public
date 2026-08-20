// Single source of truth for the origin every API call goes to.
//
// Two inputs, one answer: the operator's Settings → "API Base URL" override
// (persisted in localStorage) wins over the build-time VITE_API_BASE default.
// Both api.ts (fetch) and events.ts (SSE) resolve through here — before, the
// setting was written to localStorage and never read back, so "Settings saved"
// was a lie and every request kept going to the build-time origin.
//
// SSR-safe: `localStorage` does not exist while the static build prerenders, so
// every read is guarded and falls back to the env default instead of throwing.

export const API_BASE_STORAGE_KEY = 'hp_api_base';

// Read at call time (not module load) so a test can stub the env and so the
// value can never go stale against an HMR reload.
export function envApiBase(): string {
	return normalizeApiBase(import.meta.env.VITE_API_BASE as string | undefined);
}

// Trim and drop trailing slashes: paths are always joined as `${base}${path}`
// with a leading-slash path, so a stored "http://host:8000/" must not produce
// "http://host:8000//auth/me".
export function normalizeApiBase(value: string | null | undefined): string {
	const v = (value ?? '').trim();
	if (!v) return '';
	return v.replace(/\/+$/, '');
}

// Pure resolution rule: override first, env default second, same-origin last.
export function resolveApiBase(
	override: string | null | undefined,
	envDefault: string | null | undefined,
): string {
	return normalizeApiBase(override) || normalizeApiBase(envDefault);
}

// The operator override as stored, or null when absent/unavailable (SSR, or a
// browser with storage blocked).
export function readStoredApiBase(): string | null {
	if (typeof localStorage === 'undefined') return null;
	try {
		return localStorage.getItem(API_BASE_STORAGE_KEY);
	} catch {
		return null;
	}
}

// Persist (or clear, when blank) the operator override. Returns false when
// storage is unavailable so the caller can tell the user it did not stick.
export function writeStoredApiBase(value: string): boolean {
	if (typeof localStorage === 'undefined') return false;
	const v = normalizeApiBase(value);
	try {
		if (v) localStorage.setItem(API_BASE_STORAGE_KEY, v);
		else localStorage.removeItem(API_BASE_STORAGE_KEY);
		return true;
	} catch {
		return false;
	}
}

// The base every request must use.
export function getApiBase(): string {
	return resolveApiBase(readStoredApiBase(), envApiBase());
}
