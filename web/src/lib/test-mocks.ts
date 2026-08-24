import { readable, writable } from 'svelte/store';
import { vi } from 'vitest';

const goto = vi.fn();
const invalidate = vi.fn();
const invalidateAll = vi.fn();

// `afterNavigate`/`beforeNavigate` are lifecycle registrars, not actions: the
// layout calls them at component init, so a mock without them throws before the
// shell can render at all.
const afterNavigate = vi.fn();
const beforeNavigate = vi.fn();

vi.mock('$app/navigation', () => ({
	goto,
	invalidate,
	invalidateAll,
	afterNavigate,
	beforeNavigate,
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
			// Default to a claimed instance: that is what every page other than
			// the first-run screen is looking at.
			claimStatus: vi.fn().mockResolvedValue({ state: 'claimed' }),
			claimInstance: vi.fn(),
			listArtifacts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			planArtifact: vi.fn().mockRejectedValue(new Error('no plan in tests')),
			getArtifactBody: vi.fn().mockResolvedValue(''),
			getArtifact: vi.fn(),
			approveArtifact: vi.fn(),
			rejectArtifact: vi.fn(),
			applyArtifact: vi.fn(),
			revokeArtifact: vi.fn(),
			getHostDoc: vi.fn(),
			searchKB: vi.fn().mockResolvedValue({ results: [], total: 0 }),
			listKB: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			// The Agents page: enough of the fleet surface that the route can be
			// rendered in a test at all. Same reason sessionStore is here - almost
			// every web test used to be a pure-function $lib test because a route
			// could not be mounted.
			getDashboard: vi.fn().mockResolvedValue(null),
			listInventory: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			addHost: vi.fn(),
			forgetHost: vi.fn().mockResolvedValue({ forgotten: true }),
			refreshInventory: vi.fn(),
			bulkHosts: vi.fn(),
			listAgents: vi.fn().mockResolvedValue([]),
			listFiringAlerts: vi.fn().mockResolvedValue({ items: [] }),
			listAlertRules: vi.fn().mockResolvedValue({ items: [] }),
			createAlertRule: vi.fn(),
			deleteAlertRule: vi.fn(),
			setAlertRuleEnabled: vi.fn(),
			getHostSeries: vi.fn().mockResolvedValue({ hostname: '', metric: '', points: [] }),
			getHubToken: vi.fn(),
			getBootstrapToken: vi.fn(),
			// The enrolment window (#537): the enrol panel reads it on mount, so a
			// default that resolves keeps every OTHER page test out of it.
			getEnrolmentWindow: vi.fn().mockResolvedValue({
				open: false,
				expires_at: null,
				seconds_remaining: 0,
				fleet_empty: true,
			}),
			openEnrolmentWindow: vi.fn(),
			closeEnrolmentWindow: vi.fn(),
			revokeAgent: vi.fn().mockResolvedValue({ revoked: true, channel_closed: true }),
			forgetAgent: vi.fn().mockResolvedValue({ forgotten: true }),
			// The host page (#514 S2, S4).
			getHost: vi.fn(),
			agentInstallEligibility: vi.fn().mockResolvedValue({ eligible: false, message: 'not in test' }),
			installAgent: vi.fn(),
			getTask: vi.fn(),
			adoptHost: vi.fn(),
			ignoreHost: vi.fn(),
			enrichInventory: vi.fn(),
			getHostLatest: vi.fn().mockResolvedValue({ hostname: '', metrics: [] }),
			listAudit: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			updateHost: vi.fn(),
		},
		// Pages gate their write controls off the session's capability list, so a
		// page test cannot render without this. Admin by default: a test about
		// error states should not also be fighting a permission screen.
		sessionStore: writable({
			authenticated: true,
			token_label: 'test',
			capabilities: ['read', 'write', 'admin'],
		}),
		// Not part of `api`: a module-level helper the Inventory page imports
		// alongside it. A vi.mock factory replaces the WHOLE module, so anything
		// the page imports from $lib/api has to be here or the import throws.
		hostMetricsUrl: (base: string, hostname: string) =>
			`${base}/monitoring?host=${encodeURIComponent(hostname)}`,
		setToken: vi.fn((t: string) => _tokenStore.set(t)),
		getToken: vi.fn(() => ''),
		hasCookieSession: vi.fn(() => false),
		refreshSession: vi.fn().mockResolvedValue(null),
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

export { goto, afterNavigate };