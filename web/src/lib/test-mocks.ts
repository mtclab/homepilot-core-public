import { readable, writable } from 'svelte/store';
import { vi } from 'vitest';

const goto = vi.fn();
const invalidate = vi.fn();
const invalidateAll = vi.fn();

vi.mock('$app/navigation', () => ({
	goto,
	invalidate,
	invalidateAll,
}));

const mockPageStore = readable({
	url: new URL('http://localhost/ui/artifacts'),
	params: {},
	route: { id: '/artifacts' },
	status: 200,
	error: null,
	data: {},
});

vi.mock('$app/stores', () => ({
	page: mockPageStore,
	subscribe: mockPageStore.subscribe,
}));

vi.mock('$app/paths', () => ({
	base: '/ui',
}));

vi.mock('$lib/api', async () => {
	const { writable } = await import('svelte/store');
	const _tokenStore = writable('');
	return {
		api: {
			me: vi.fn().mockResolvedValue({ authenticated: true, token_label: 'test' }),
			login: vi.fn().mockResolvedValue({ status: 'ok' }),
			logout: vi.fn().mockResolvedValue({ status: 'ok' }),
			listArtifacts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			getArtifact: vi.fn(),
			approveArtifact: vi.fn(),
			rejectArtifact: vi.fn(),
			applyArtifact: vi.fn(),
			revokeArtifact: vi.fn(),
			listInventory: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			refreshInventory: vi.fn(),
			getHost: vi.fn(),
			getHostDoc: vi.fn(),
			searchKB: vi.fn().mockResolvedValue({ results: [], total: 0 }),
			listKB: vi.fn().mockResolvedValue({ items: [], total: 0 }),
		},
		setToken: vi.fn((t: string) => _tokenStore.set(t)),
		getToken: vi.fn(() => ''),
		hasCookieSession: vi.fn(() => false),
		hasSession: vi.fn(() => Promise.resolve(false)),
	};
});

vi.mock('$lib/stores', async () => {
	const { writable } = await import('svelte/store');
	const toastStore = writable<{ msg: string; kind: 'ok' | 'err' } | null>(null);
	return {
		toast: toastStore,
		notify: vi.fn(),
	};
});

export { goto };