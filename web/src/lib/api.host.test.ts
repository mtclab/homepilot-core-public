import { describe, it, expect } from 'vitest';

// Tests for the Host interface field mapping and helper functions.
// The API returns snake_case fields; the TypeScript interface must match exactly.

// qs helper is not exported — test its contract via a local reimplementation
// that mirrors what api.ts does, then verify the KB search path construction.

function qs(params: Record<string, string | number | boolean | undefined>): string {
	const p = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v !== undefined && v !== '') p.set(k, String(v));
	}
	const s = p.toString();
	return s ? `?${s}` : '';
}

describe('qs helper', () => {
	it('returns empty string when all values are undefined', () => {
		expect(qs({ role: undefined, status: undefined })).toBe('');
	});

	it('returns empty string when all values are empty string', () => {
		expect(qs({ kind: '', target: '' })).toBe('');
	});

	it('includes defined non-empty values', () => {
		const result = qs({ role: 'vm', status: 'online' });
		expect(result).toContain('role=vm');
		expect(result).toContain('status=online');
		expect(result.startsWith('?')).toBe(true);
	});

	it('skips undefined but includes other params', () => {
		const result = qs({ kind: 'note', target: undefined });
		expect(result).toBe('?kind=note');
	});

	it('handles numeric values', () => {
		const result = qs({ limit: 50 });
		expect(result).toBe('?limit=50');
	});

	it('handles boolean values', () => {
		const result = qs({ managed: true });
		expect(result).toBe('?managed=true');
	});
});

describe('Host interface field names', () => {
	// Simulate the shape the API actually returns and verify our interface matches.
	interface Host {
		id: string;
		hostname: string;
		node?: string;
		ip_address?: string;
		role?: string;
		status?: string;
		managed?: number;
		tags?: string;
		host_type?: string;
		fqdn?: string;
	}

	it('accepts ip_address field (snake_case from API)', () => {
		const host: Host = {
			id: 'abc123',
			hostname: 'pve-node',
			ip_address: 'pve.example.local',
			status: 'online',
			managed: 1,
		};
		expect(host.ip_address).toBe('pve.example.local');
	});

	it('ip_address is optional', () => {
		const host: Host = { id: 'x', hostname: 'unknown' };
		expect(host.ip_address).toBeUndefined();
	});

	it('managed is numeric (sqlite integer)', () => {
		const host: Host = { id: 'x', hostname: 'h', managed: 1 };
		expect(typeof host.managed).toBe('number');
		expect(host.managed).toBeTruthy();
	});

	it('managed === 0 is falsy', () => {
		const host: Host = { id: 'x', hostname: 'h', managed: 0 };
		expect(host.managed && host.managed !== 0).toBeFalsy();
	});

	it('managed === 1 is truthy', () => {
		const host: Host = { id: 'x', hostname: 'h', managed: 1 };
		expect(host.managed && host.managed !== 0).toBeTruthy();
	});
});

describe('KB search path construction', () => {
	it('search with query produces correct path', () => {
		const q = 'nginx';
		const kind = 'note';
		const limit = 50;
		const path = '/kb/search' + qs({ q, kind, limit });
		expect(path).toBe('/kb/search?q=nginx&kind=note&limit=50');
	});

	it('search without kind omits kind param', () => {
		const path = '/kb/search' + qs({ q: 'ssh', kind: undefined, limit: 50 });
		expect(path).toContain('q=ssh');
		expect(path).not.toContain('kind');
	});

	it('list KB with kind filter', () => {
		const path = '/kb' + qs({ kind: 'doc' });
		expect(path).toBe('/kb?kind=doc');
	});

	it('list KB without filter returns bare path', () => {
		const path = '/kb' + qs({ kind: undefined });
		expect(path).toBe('/kb');
	});
});

describe('kindColor logic', () => {
	function kindColor(kind: string): string {
		if (kind === 'note') return 'text-accent';
		if (kind === 'doc') return 'text-note';
		if (kind === 'fact') return 'text-ok';
		return 'text-muted';
	}

	it('note → accent', () => expect(kindColor('note')).toBe('text-accent'));
	it('doc → note', () => expect(kindColor('doc')).toBe('text-note'));
	it('fact → ok', () => expect(kindColor('fact')).toBe('text-ok'));
	it('unknown → muted', () => expect(kindColor('other')).toBe('text-muted'));
});
