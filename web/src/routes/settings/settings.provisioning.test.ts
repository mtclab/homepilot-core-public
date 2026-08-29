import { render, waitFor, cleanup, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { api, ApiError } from '$lib/api';

/**
 * THE C3 GATE (#553), UI half: the provisioning defaults are configurable from
 * the product, and the CLUSTER'S OWN ANSWER is what the operator reads - before
 * a save (Test) and when a save is refused (422).
 *
 * The defects this forbids:
 *  - the provisioning defaults having no home on this surface at all, leaving
 *    node/bridge/VLAN reachable only through env vars;
 *  - a refusal collapsed into "Request failed", hiding the one sentence that
 *    says WHICH bridges the node actually has;
 *  - a Test button that saves, or one offered for a setting with no probe
 *    behind it (a check that never happened, reported as "ok").
 *
 * Teeth: drop the provisioning group from settingFields.ts and the card case
 * fails; swallow the ApiError detail and the refusal case fails; render Test
 * unconditionally and the "only where a probe exists" case fails; call
 * saveSettingOverride from the Test handler and the "asks without saving" case
 * fails.
 */
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const listSettingOverrides = api.listSettingOverrides as ReturnType<typeof vi.fn>;
const saveSettingOverride = api.saveSettingOverride as ReturnType<typeof vi.fn>;
const probeSettingOverride = api.probeSettingOverride as ReturnType<typeof vi.fn>;

const REPORT = {
	timeout_seconds: 2,
	counts: { ok: 0, off: 1, unreachable: 0, unknown: 0 },
	subsystems: [
		{
			name: 'embeddings',
			configured: false,
			state: 'off',
			target: '',
			consequence: 'KB search is keyword-only because no embedding service is configured.',
		},
	],
};

function setting(key: string, extra: Record<string, unknown> = {}) {
	return {
		key,
		value: '',
		source: 'default',
		type: 'str',
		hot_reloadable: true,
		description: `The ${key}.`,
		env_var: `HP_${key.toUpperCase()}`,
		editable: true,
		probeable: true,
		...extra,
	};
}

const OVERRIDES = [
	// A C2 setting with no probe, so "Test only where there is something to
	// test" is asserted against a real neighbour rather than a hypothetical.
	setting('retention_days', { value: 90, type: 'int', probeable: false }),
	setting('provision_default_node', { value: 'pve1', source: 'db' }),
	setting('provision_default_template_vmid', { value: 9000, type: 'int', source: 'db' }),
	setting('provision_default_pool', { value: 'guests', source: 'db' }),
	setting('provision_default_storage', { value: '', source: 'default' }),
	setting('provision_default_bridge', { value: 'vmbr0', source: 'db' }),
	setting('provision_default_vlan_tag', { value: 0, type: 'int' }),
	setting('provision_default_ipconfig', { value: 'ip=dhcp' }),
	setting('provision_ip_mode', { value: 'static' }),
	setting('provision_default_nameserver', { value: '1.1.1.1' }),
];

function field(key: string): HTMLElement {
	const el = document.querySelector(`[data-setting="${key}"]`);
	if (!(el instanceof HTMLElement)) throw new Error(`no field for ${key}`);
	return el;
}

describe('Subsystems tab: provisioning defaults, checked against the cluster', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue(REPORT);
		listSettingOverrides.mockResolvedValue({ settings: OVERRIDES });
		saveSettingOverride.mockResolvedValue({ status: 'ok', probe: null });
		probeSettingOverride.mockResolvedValue({
			key: 'provision_default_bridge',
			ok: true,
			reachable: true,
			detail: 'Bridge vmbr0 is on node pve1.',
		});
	});

	it('gives the provisioning defaults a card of their own', async () => {
		render(SettingsPage);
		const bridge = await waitFor(() => field('provision_default_bridge'));
		const card = bridge.closest('.card');
		expect(card).toBeTruthy();
		expect(card!.textContent).toContain('Provisioning defaults');
		// Every one of them, on the one card - a default reachable only
		// through an env var is the state C3 exists to end, and the address
		// mode (#630) is exactly the kind of decision that must not hide in a
		// .env file: it is what stands between a friend and a guest with no
		// address at all.
		for (const key of [
			'provision_default_node',
			'provision_default_template_vmid',
			'provision_default_pool',
			'provision_default_storage',
			'provision_default_bridge',
			'provision_default_vlan_tag',
			'provision_default_ipconfig',
			'provision_ip_mode',
			'provision_default_nameserver',
		]) {
			expect(card!.contains(field(key))).toBe(true);
		}
	});

	it('asks the cluster on Test and shows its answer, without saving', async () => {
		render(SettingsPage);
		await waitFor(() => field('provision_default_bridge'));

		const input = within(field('provision_default_bridge')).getByRole('textbox');
		await userEvent.clear(input);
		await userEvent.type(input, 'vmbr7');
		await userEvent.click(
			within(field('provision_default_bridge')).getByRole('button', { name: 'Test' }),
		);

		await waitFor(() =>
			expect(probeSettingOverride).toHaveBeenCalledWith('provision_default_bridge', 'vmbr7'),
		);
		// Test is a question, not a write.
		expect(saveSettingOverride).not.toHaveBeenCalled();
		await waitFor(() =>
			expect(field('provision_default_bridge').textContent).toContain(
				'Bridge vmbr0 is on node pve1.',
			),
		);
	});

	it("shows the cluster's refusal verbatim, listing what the node does have", async () => {
		probeSettingOverride.mockResolvedValue({
			key: 'provision_default_bridge',
			ok: false,
			reachable: true,
			detail: 'no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1',
		});
		render(SettingsPage);
		await waitFor(() => field('provision_default_bridge'));

		await userEvent.click(
			within(field('provision_default_bridge')).getByRole('button', { name: 'Test' }),
		);

		const probe = await waitFor(() =>
			field('provision_default_bridge').querySelector('[data-probe]'),
		);
		expect(probe!.textContent).toContain('no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1');
		expect(probe!.className).toContain('text-danger');
	});

	it('shows a refused SAVE in the cluster’s own words, not "request failed"', async () => {
		saveSettingOverride.mockRejectedValue(
			new ApiError(
				422,
				JSON.stringify({ detail: 'no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1' }),
			),
		);
		render(SettingsPage);
		await waitFor(() => field('provision_default_bridge'));

		await userEvent.click(
			within(field('provision_default_bridge')).getByRole('button', { name: 'Save' }),
		);

		await waitFor(() =>
			expect(field('provision_default_bridge').textContent).toContain(
				'no bridge vmbr7 on node pve1; node has: vmbr0, vmbr1',
			),
		);
	});

	it('keeps a caveat the server attached to a successful save', async () => {
		saveSettingOverride.mockResolvedValue({
			status: 'ok',
			key: 'provision_default_vlan_tag',
			value: 42,
			source: 'db',
			probe: {
				ok: true,
				detail:
					'Saved, but unverified: node pve1 does not report whether bridge vmbr0 is VLAN-aware.',
			},
		});
		render(SettingsPage);
		await waitFor(() => field('provision_default_vlan_tag'));

		await userEvent.click(
			within(field('provision_default_vlan_tag')).getByRole('button', { name: 'Save' }),
		);

		await waitFor(() =>
			expect(field('provision_default_vlan_tag').textContent).toContain(
				'Saved, but unverified: node pve1 does not report whether bridge vmbr0 is VLAN-aware.',
			),
		);
	});

	it('offers Test only where the server says there is a probe', async () => {
		render(SettingsPage);
		await waitFor(() => field('retention_days'));
		expect(within(field('retention_days')).queryByRole('button', { name: 'Test' })).toBeNull();
		expect(
			within(field('provision_default_node')).getByRole('button', { name: 'Test' }),
		).toBeTruthy();
	});

	it('renders an env-locked provisioning default read-only, with no Test to mislead', async () => {
		listSettingOverrides.mockResolvedValue({
			settings: [
				setting('provision_default_node', { value: 'pve9', source: 'env', editable: false }),
			],
		});
		render(SettingsPage);
		const node = await waitFor(() => field('provision_default_node'));
		expect(within(node).queryAllByRole('button')).toHaveLength(0);
		expect(node.textContent).toContain(
			'Set by HP_PROVISION_DEFAULT_NODE in the environment, which wins over anything saved here',
		);
		expect(node.textContent).toContain('pve9');
	});
});
