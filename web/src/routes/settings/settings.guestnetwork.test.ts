import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { api } from '$lib/api';

/**
 * The Guest network card (#553): survey vs desired vs plan, told truthfully.
 *
 * The card exists to make one decision an informed one - "should I rebuild the
 * guest subnet" - and the ways it could lie are exactly what is gated here:
 *
 *  - a converged cluster shown as "3 steps pending", or the reverse;
 *  - a blocked plan shown as a list of steps that will never run;
 *  - the legacy-stack caveat missing, so the page implies the vnet firewall
 *    fences a guest when on this estate's firewall stack it does not;
 *  - an Apply button, which would be a second way to change the estate that
 *    leaves no record of intent (the change ships as an artifact).
 *
 * Teeth (each proven by reverting and watching the NAMED case fail):
 *  - render `plan.steps.length` without checking `converged` -> the converged
 *    case fails;
 *  - drop the enforcement line -> the legacy-caveat case fails;
 *  - add a mutate button -> the no-button case fails;
 *  - drop `aria-describedby` from SettingFields -> the explains-walk case fails.
 */

const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const listSettingOverrides = api.listSettingOverrides as ReturnType<typeof vi.fn>;
const getGuestNetwork = api.getGuestNetwork as ReturnType<typeof vi.fn>;

function panel(): HTMLElement {
	const el = document.getElementById('settings-panel');
	if (!(el instanceof HTMLElement)) throw new Error('no settings panel');
	return el;
}

function card(): HTMLElement {
	const el = document.querySelector('[data-guest-network]');
	if (!(el instanceof HTMLElement)) throw new Error('no guest-network card');
	return el;
}

const DESIRED = {
	zone: 'guest',
	vnet: 'innkeep',
	subnet_cidr: '10.96.17.0/24',
	gateway: '10.96.17.1',
	snat: true,
	dhcp: true,
	dhcp_range: '10.96.17.100-10.96.17.199',
	dhcp_dns_server: '',
	isolate_cidrs: ['10.0.0.1/24'],
};

const SURVEY = {
	zones: [],
	vnets: [],
	subnets: [],
	node: 'elizabeth',
	firewall_stack: 'legacy',
	pending: [],
	errors: [],
};

const LEGACY_NOTE =
	'Node elizabeth runs the LEGACY iptables firewall, which stores vnet firewall rules ' +
	'but does not enforce them on vnet forward traffic. The fence that holds today is the ' +
	'per-VM rule set HomePilot writes at provision time.';

const SETTINGS = [
	{
		key: 'guest_network_subnet',
		value: '10.96.17.0/24',
		source: 'db',
		type: 'str',
		hot_reloadable: true,
		description: 'The guest subnet in CIDR form, e.g. 10.96.17.0/24. Empty means no guest network.',
		env_var: 'HP_GUEST_NETWORK_SUBNET',
		editable: true,
		probeable: true,
	},
	{
		key: 'guest_network_isolate_cidrs',
		value: '10.0.0.1/24',
		source: 'default',
		type: 'str',
		hot_reloadable: true,
		description:
			'The networks a guest must never reach. This is the fence: every provisioned guest gets a per-VM DROP towards each of these.',
		env_var: 'HP_GUEST_NETWORK_ISOLATE_CIDRS',
		editable: true,
		probeable: true,
	},
];

describe('the Guest network card', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue({
			timeout_seconds: 2,
			counts: {},
			subsystems: [],
		});
		listSettingOverrides.mockResolvedValue({ settings: SETTINGS });
	});

	it('says the network is not described yet on a fresh install', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: false,
			desired: null,
			survey: null,
			plan: null,
			detail:
				'No guest network is configured on this instance. Set guest_network_subnet and guest_network_gateway on Settings -> Subsystems -> Guest network.',
			enforcement: '',
		});
		render(SettingsPage);
		await waitFor(() => expect(getGuestNetwork).toHaveBeenCalled());
		const state = await waitFor(() => {
			const el = document.querySelector('[data-guest-network-state]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(state.textContent).toMatch(/Not described yet/i);
		expect(state.textContent).toMatch(/guest_network_subnet/);
		// Nothing to run, so no plan is shown - not an empty list pretending to be one.
		expect(document.querySelector('[data-guest-network-plan]')).toBeNull();
	});

	it('counts the pending steps and lists them in the order they would run', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: {
				converged: false,
				blockers: [],
				steps: [
					{ id: 'create-zone', description: 'create SDN zone guest', op: 'create_zone', params: {} },
					{ id: 'create-vnet', description: 'create vnet innkeep in zone guest', op: 'create_vnet', params: {} },
					{ id: 'apply-sdn', description: 'apply the pending SDN configuration', op: 'apply_sdn', params: {} },
				],
			},
			detail: '3 step(s) pending.',
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		const state = await waitFor(() => {
			const el = document.querySelector('[data-guest-network-state]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(state.textContent).toMatch(/3 steps pending/i);
		const items = Array.from(document.querySelectorAll('[data-guest-network-plan] li'));
		expect(items.map((li) => li.textContent?.trim().split(/\s+/)[0])).toEqual([
			'create-zone',
			'create-vnet',
			'apply-sdn',
		]);
	});

	it('says converged when the cluster already matches, and shows no plan', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: { ...SURVEY, firewall_stack: 'nftables' },
			plan: { converged: true, blockers: [], steps: [] },
			detail: 'The cluster matches the desired guest network.',
			enforcement: 'Node elizabeth runs the nftables proxmox-firewall, so the vnet forward rules are enforced.',
		});
		render(SettingsPage);
		const state = await waitFor(() => {
			const el = document.querySelector('[data-guest-network-state]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(state.textContent).toMatch(/Converged/i);
		expect(document.querySelector('[data-guest-network-plan]')).toBeNull();
	});

	it('shows a blocker as a refusal, never as steps to run', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: {
				converged: false,
				blockers: ["zone guest already exists with type 'vxlan', not 'simple'."],
				steps: [],
			},
			detail: "zone guest already exists with type 'vxlan', not 'simple'.",
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		const state = await waitFor(() => {
			const el = document.querySelector('[data-guest-network-state]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(state.textContent).toMatch(/Cannot proceed/i);
		expect(state.textContent).toMatch(/vxlan/);
		expect(document.querySelector('[data-guest-network-plan]')).toBeNull();
	});

	it('states the legacy-stack caveat, because it decides what the fence is worth', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: { converged: true, blockers: [], steps: [] },
			detail: 'The cluster matches the desired guest network.',
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		const note = await waitFor(() => {
			const el = document.querySelector('[data-guest-network-enforcement]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(note.textContent).toMatch(/LEGACY/);
		expect(note.textContent).toMatch(/does not enforce them/i);
		expect(note.textContent).toMatch(/per-VM rule set/i);
	});

	it('points at the artifact path and offers NO way to mutate from here', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: {
				converged: false,
				blockers: [],
				steps: [{ id: 'create-zone', description: 'create SDN zone guest', op: 'create_zone', params: {} }],
			},
			detail: '1 step(s) pending.',
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		await waitFor(() => expect(getGuestNetwork).toHaveBeenCalled());
		const text = card().textContent ?? '';
		expect(text).toMatch(/guest-network/);
		expect(text).toMatch(/approve it with the\s+code/i);
		const link = card().querySelector('a[href="/ui/changes/review"]');
		expect(link, 'the card must point at the review queue').toBeTruthy();

		// No button on this card changes the estate. Re-check and the per-setting
		// Save/Test/Reset are the whole button budget.
		const buttons = Array.from(card().querySelectorAll('button')).map((b) =>
			(b.textContent ?? '').trim(),
		);
		expect(buttons.filter((label) => /apply|build|create|converge/i.test(label))).toEqual([]);
	});

	it('reports a survey that could not be read instead of implying an empty cluster', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: { ...SURVEY, errors: ['could not list the SDN zones: 401 no ticket'] },
			plan: { converged: false, blockers: [], steps: [] },
			detail: '0 step(s) pending.',
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		await waitFor(() => expect(getGuestNetwork).toHaveBeenCalled());
		expect(card().textContent).toMatch(/401 no ticket/);
		expect(card().textContent).toMatch(/the plan above is incomplete/i);
	});

	it('renders the guest-network settings on the card that explains them', async () => {
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: { converged: true, blockers: [], steps: [] },
			detail: 'ok',
			enforcement: LEGACY_NOTE,
		});
		render(SettingsPage);
		await waitFor(() => expect(listSettingOverrides).toHaveBeenCalled());
		await waitFor(() => {
			const field = card().querySelector('[data-setting="guest_network_isolate_cidrs"]');
			if (!field) throw new Error('not yet');
			expect(field.textContent).toMatch(/This is the fence/i);
		});
	});
});

describe('F7: the Subsystems tab explains every value it asks for', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue({ timeout_seconds: 2, counts: {}, subsystems: [] });
		listSettingOverrides.mockResolvedValue({ settings: SETTINGS });
		getGuestNetwork.mockResolvedValue({
			configured: true,
			desired: DESIRED,
			survey: SURVEY,
			plan: { converged: true, blockers: [], steps: [] },
			detail: 'ok',
			enforcement: LEGACY_NOTE,
		});
	});

	/**
	 * The WALK, not a list of field names: a setting added next year with no
	 * description fails this without anyone remembering to extend the test. That
	 * is the only way explanations do not silently fall behind the form.
	 */
	it('gives EVERY input on the tab its own attached explanation', async () => {
		render(SettingsPage);
		await waitFor(() => expect(listSettingOverrides).toHaveBeenCalled());
		await waitFor(() => {
			if (!panel().querySelector('input')) throw new Error('not yet');
		});

		const controls = Array.from(panel().querySelectorAll('input, select, textarea'));
		expect(controls.length, 'a tab with no inputs would pass vacuously').toBeGreaterThan(0);

		const undescribed = controls.filter((control) => {
			const id = control.getAttribute('aria-describedby');
			if (!id) return true;
			const note = id
				.split(/\s+/)
				.map((one) => document.getElementById(one)?.textContent?.trim() ?? '')
				.join(' ')
				.trim();
			return note.length < 20;
		});
		expect(
			undescribed.map((c) => c.getAttribute('id') ?? c.outerHTML.slice(0, 80)),
			'every field must say what it is for, attached to the field itself',
		).toEqual([]);

		// And every input is labelled, so the description has something to describe.
		const unlabelled = controls.filter((c) => {
			const id = c.getAttribute('id');
			return !id || !document.querySelector(`label[for="${id}"]`);
		});
		expect(unlabelled.map((c) => c.getAttribute('id'))).toEqual([]);
	});

	it('leads in before it asks: the card says what a guest network IS', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuestNetwork).toHaveBeenCalled());
		const text = card().textContent ?? '';
		expect(text).toMatch(/subnet a friend/i);
		expect(text).toMatch(/must never reach/i);
		expect(text).toMatch(/that is the fence/i);
	});
});
