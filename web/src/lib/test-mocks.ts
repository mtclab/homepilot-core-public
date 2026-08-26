import { writable } from 'svelte/store';
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

// Writable, not readable: a page whose SECTOR is `?tab=` state (the host page,
// #549 F3) is about the URL, so a test of it has to be able to say which URL it
// is at. The default is unchanged, so every page test that does not care sees
// exactly what it saw before. Vitest isolates modules per test FILE, so setting
// it cannot leak into another file - but reset it in `beforeEach` within one.
const mockPageStore = writable({
	url: new URL('http://localhost/ui/artifacts'),
	params: {} as Record<string, string>,
	route: { id: '/artifacts' },
	status: 200,
	error: null,
	data: {},
});

/** Put the mocked `$page` at a URL (path + query + hash), with route params. */
export function setPage(pathAndQuery: string, params: Record<string, string> = {}): void {
	const url = new URL('http://localhost' + pathAndQuery);
	mockPageStore.set({
		url,
		params,
		route: { id: url.pathname },
		status: 200,
		error: null,
		data: {},
	});
}

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
	// The REAL error class, not a stand-in: components branch on it to decide
	// whether they have the server's own sentence to show (#553 C2 surfaces a
	// 409 verbatim), and a mock class would make that branch untestable.
	const { ApiError } = await vi.importActual<typeof import('./api')>('$lib/api');
	return {
		ApiError,
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
			resetApprovalCode: vi.fn(),
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
			// Overview's "recent movement" + attention zones and Records → Tasks
			// read the task list (#549 F2/F5). Empty by default so every OTHER
			// page test is unaffected.
			listTasks: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			cancelTask: vi.fn(),
			updateHost: vi.fn(),
			// Drift (#549 F4): the Drift tab asks for the check rows on mount, so
			// a default that resolves keeps other page tests out of it.
			getDriftStatus: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			recheckDrift: vi.fn().mockResolvedValue({ items: [] }),
			// Settings (#549 F6). Defaults that RESOLVE, so a test of one tab is
			// not fighting the load of the six others; the Subsystems default is an
			// empty report rather than a seeded one, because a test that asserts a
			// subsystem card must seed the subsystem it is about.
			getHealth: vi.fn().mockResolvedValue({ status: 'ok', version: 'test', checks: {} }),
			getSelfcheck: vi
				.fn()
				.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] }),
			// #553 C2: the Subsystems tab loads the editable settings alongside the
			// self-check. Empty by default for the same reason the report is - a
			// test that asserts an edit control seeds the setting it is about.
			listSettingOverrides: vi.fn().mockResolvedValue({ settings: [] }),
			saveSettingOverride: vi.fn().mockResolvedValue({ status: 'ok' }),
			clearSettingOverride: vi.fn().mockResolvedValue({ status: 'ok' }),
			// #553 C3: "Test" asks the cluster without saving. The default answer is
			// a bland ok - a test about a refusal seeds the refusal it is about.
			probeSettingOverride: vi
				.fn()
				.mockResolvedValue({ key: '', ok: true, reachable: true, detail: 'ok' }),
			getProxmoxSettings: vi.fn().mockResolvedValue({
				host: '',
				port: 8006,
				verify_ssl: true,
				token_configured: false,
				write_token_configured: false,
				connection_status: 'not_configured',
			}),
			saveProxmoxSettings: vi.fn().mockResolvedValue({ status: 'ok', reloaded: [] }),
			testProxmoxConnection: vi.fn().mockResolvedValue({ status: 'ok', message: 'ok' }),
			getGuests: vi.fn().mockResolvedValue({ guests: [], invites: [] }),
			mintGuestInvite: vi.fn(),
			setGuestQuota: vi.fn(),
			revokeGuestInvite: vi.fn(),
			listTokens: vi.fn().mockResolvedValue({ items: [], total: 0 }),
			createToken: vi.fn(),
			revokeToken: vi.fn(),
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
		hostMetricsUrl: (base: string, hostname: string, hostId?: string) =>
			hostId
				? `${base}/hosts/${encodeURIComponent(hostId)}?tab=metrics`
				: `${base}/inventory?q=${encodeURIComponent(hostname)}`,
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