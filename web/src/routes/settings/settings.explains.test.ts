import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
// UI slice 1 moved three sectors out of Settings. The F7 gate follows the
// panels to their new pages: the explanations are the panels', not the
// route's, and must survive the move.
import GuestsPage from '../guests/+page.svelte';
import MonitoringRulesPage from '../monitoring/rules/+page.svelte';
import AssistantsPage from '../assistants/+page.svelte';
import { api } from '$lib/api';

/**
 * THE F7 GATE (#549): Settings explains every value it asks for.
 *
 * Owner, 2026-08-26, after using the shipped 3.5.0 UI: "The Guests tab is
 * missing all the necessary information, it just has values to set but no
 * explanation what each value is for... things aren't clear/self explanatory."
 *
 * The standing assertion is the WALK: every input the Guests tab renders is
 * looked up and required to carry a description. It is deliberately not a list
 * of field names - a field added next year with no explanation fails this test
 * without anyone remembering to extend it, which is the only way explanations
 * do not silently fall behind the form.
 *
 * Teeth (all four proven by reverting and watching the failure):
 *  - drop one `aria-describedby` (or empty its note) -> the walk fails naming
 *    the field;
 *  - restore the old "template/node are required" copy -> the optional-defaults
 *    case fails;
 *  - shorten envLockNote back to "set by HP_X in the environment" -> the
 *    unlock-sentence case fails;
 *  - delete a tab's lead-in paragraph -> that tab's case fails.
 */
const mintGuestInvite = api.mintGuestInvite as ReturnType<typeof vi.fn>;
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const listSettingOverrides = api.listSettingOverrides as ReturnType<typeof vi.fn>;
const listTokens = api.listTokens as ReturnType<typeof vi.fn>;

/** The rendered panel: Settings' tab panel, or a moved sector's page region. */
function panel(id = 'settings-panel'): HTMLElement {
	const el = document.getElementById(id);
	if (!(el instanceof HTMLElement)) throw new Error(`no ${id}`);
	return el;
}

/**
 * The description attached to a control, resolved the way a screen reader would
 * resolve it: `aria-describedby` -> the element it names -> its text.
 */
function describedBy(control: Element): string {
	const id = control.getAttribute('aria-describedby');
	if (!id) return '';
	return id
		.split(/\s+/)
		.map((one) => document.getElementById(one)?.textContent?.trim() ?? '')
		.join(' ')
		.trim();
}

/** How a failure names the offending control, since it has no description to quote. */
function nameOf(control: Element): string {
	const id = control.getAttribute('id');
	if (id) {
		const label = document.querySelector(`label[for="${id}"]`);
		if (label?.textContent) return `${id} (${label.textContent.trim()})`;
		return id;
	}
	return control.outerHTML.slice(0, 120);
}

describe('F7: the Guests page explains every value it asks for', () => {
	// 3.12 moved the two always-open forms into dialogs and the two paragraphs
	// behind "How the guest portal works". The gate follows them: every field
	// either dialog asks for still carries its own description, wired the way a
	// screen reader reads it - now one line, not a paragraph.
	const getGuestFriends = api.getGuestFriends as ReturnType<typeof vi.fn>;
	const sizing = {
		presets: [
			{ name: 'small', cores: 1, memory_mb: 1024, disk_gb: 20 },
			{ name: 'medium', cores: 2, memory_mb: 2048, disk_gb: 20 },
		],
		bounds: { cores: { min: 1, max: 32 }, memory_mb: { min: 256, max: 65536 }, disk_gb: { min: 1, max: 2000 } },
		ttl_days: { min: 1, max: 90, default: 7 },
	};
	const friend = {
		cn: 'friend.example',
		machines: [],
		usage: { vms: 0, cores: 0, memory_mb: 0, disk_gb: 0 },
		limits: { vms: 1, cores: null, memory_mb: null, disk_gb: null },
		invites: [],
	};
	function seed(defaults = { node: 'pve1', template_vmid: 9000 }) {
		getGuestFriends.mockResolvedValue({ friends: [friend], sizing, defaults, portal_origin: null });
	}
	async function openInvite() {
		render(GuestsPage);
		await userEvent.click(await screen.findByRole('button', { name: 'Invite a friend' }));
		const dlg = await screen.findByTestId('invite-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Custom' }));
		const more = within(dlg).queryByRole('button', { name: /more options/i });
		if (more) await userEvent.click(more);
		return dlg;
	}

	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/guests');
		seed();
	});

	it('says what the guest portal actually is, one click away', async () => {
		render(GuestsPage);
		await waitFor(() => expect(getGuestFriends).toHaveBeenCalled());
		const summary = await screen.findByText('How the guest portal works');
		const text = summary.closest('details')?.textContent ?? '';
		// A friend, a certificate, a machine, a budget: the four nouns an operator
		// needs before any of the boxes mean anything.
		expect(text).toMatch(/client certificate/i);
		expect(text).toMatch(/guest portal/i);
		expect(text).toMatch(/budget\s+caps\s+the\s+total/i);
	});

	it('gives EVERY input in both dialogs its own explanation', async () => {
		// Walked while each dialog is open: a closed dialog's descriptions are
		// gone from the document with it.
		function walk(scope: HTMLElement): { count: number; undescribed: string[]; unlabelled: string[] } {
			const controls = Array.from(scope.querySelectorAll('input, select, textarea'));
			return {
				count: controls.length,
				undescribed: controls.filter((c) => describedBy(c).length < 20).map(nameOf),
				unlabelled: controls
					.filter((c) => {
						const id = c.getAttribute('id');
						return !id || !document.querySelector(`label[for="${id}"]`);
					})
					.map(nameOf),
			};
		}
		const dlg = await openInvite();
		const invite = walk(dlg);
		await userEvent.click(within(dlg).getByRole('button', { name: 'Cancel' }));
		await userEvent.click(await screen.findByRole('button', { name: 'Budget for friend.example' }));
		const budget = walk(await screen.findByTestId('budget-dialog'));
		// cn, cores, memory, disk, ttl, template, node + four budget axes. A
		// dialog that renders no inputs would pass a per-input walk vacuously.
		expect(invite.count).toBeGreaterThanOrEqual(7);
		expect(budget.count).toBe(4);
		expect([...invite.undescribed, ...budget.undescribed]).toEqual([]);
		expect([...invite.unlabelled, ...budget.unlabelled]).toEqual([]);
	});

	it('says that an empty node or template means the Provisioning default', async () => {
		const dlg = await openInvite();
		for (const name of ['Template VMID', 'Node']) {
			const note = describedBy(within(dlg).getByLabelText(name));
			expect(note, name).toMatch(/leave it empty/i);
			expect(note, name).toMatch(/provisioning default/i);
		}
	});

	it('with no default set, opens More options itself and points at where defaults are set', async () => {
		seed({ node: null, template_vmid: null } as never);
		render(GuestsPage);
		await userEvent.click(await screen.findByRole('button', { name: 'Invite a friend' }));
		const dlg = await screen.findByTestId('invite-dialog');
		const note = describedBy(within(dlg).getByLabelText('Template VMID'));
		expect(note).toMatch(/no provisioning default is set/i);
		const link = dlg.querySelector('a[href="/ui/settings?tab=subsystems"]');
		expect(link?.textContent).toMatch(/subsystems/i);
	});

	it('separates the per-machine size from the friend-wide budget', async () => {
		const dlg = await openInvite();
		expect(describedBy(within(dlg).getByLabelText('Cores'))).toMatch(/this one machine/i);
		await userEvent.click(within(dlg).getByRole('button', { name: 'Cancel' }));
		await userEvent.click(await screen.findByRole('button', { name: 'Budget for friend.example' }));
		const budget = await screen.findByTestId('budget-dialog');
		expect(describedBy(within(budget).getByLabelText('Cores'))).toMatch(/across all their machines/i);
		// Lowering a budget is the question an operator asks before typing a
		// smaller number, so the answer is on the form, not in the docs.
		expect(budget.textContent).toMatch(/never touches machines that already exist/i);
	});

	it('explains how long the link lives, and offers it as a field at all', async () => {
		const dlg = await openInvite();
		const note = describedBy(within(dlg).getByLabelText('Link valid for'));
		expect(note).toMatch(/stops working/i);
		expect(note).toMatch(/once/i);
	});

	it('mints with no node or template, and shows which defaults the invite got', async () => {
		mintGuestInvite.mockResolvedValue({
			id: 'inv1',
			token: 'hpi_abcdabcdabcd.secret',
			cn: 'friend.example',
			caps: { node: 'pve1', template_vmid: 9000, pool: null, ipconfig0: null },
		});
		render(GuestsPage);
		await userEvent.click(await screen.findByRole('button', { name: 'Invite a friend' }));
		const dlg = await screen.findByTestId('invite-dialog');
		await userEvent.type(within(dlg).getByLabelText('Certificate name'), 'friend.example');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Create invite link' }));

		await waitFor(() => expect(mintGuestInvite).toHaveBeenCalled());
		expect(mintGuestInvite.mock.calls[0][0]).toMatchObject({
			cn: 'friend.example',
			node: null,
			template_vmid: null,
		});
		const caps = await waitFor(() => {
			const el = document.querySelector('[data-minted-caps]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(caps.textContent).toMatch(/template 9000/);
		expect(caps.textContent).toMatch(/pve1/);
		expect(document.querySelector('[data-invite-link-panel]')?.textContent).toMatch(/only time the link is shown/i);
	});
});

describe('F7: the other Settings sectors lead in before they ask', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('Monitoring says what a rule does and where an alert surfaces', async () => {
		setPage('/ui/monitoring/rules');
		render(MonitoringRulesPage);
		await waitFor(() => expect(api.listAlertRules).toHaveBeenCalled());
		const text = panel('monitoring-rules').textContent ?? '';
		expect(text).toMatch(/held for the whole duration/i);
		expect(text).toMatch(/Overview and on the affected host/i);
		// And the compact rule row is explained as the sentence it is.
		const rowNote = panel('monitoring-rules').querySelector('[data-alert-form-note]');
		expect(rowNote?.textContent).toMatch(/fires when/i);
	});

	it('Tokens says what a token is for, the scope ladder, and the shown-once rule', async () => {
		setPage('/ui/assistants');
		listTokens.mockResolvedValue({ items: [], total: 0 });
		render(AssistantsPage);
		await waitFor(() => expect(listTokens).toHaveBeenCalled());
		const text = panel('assistants-panel').textContent ?? '';
		expect(text).toMatch(/not a browser signs in/i);
		expect(text).toMatch(/read_only/);
		expect(text).toMatch(/admin/);
		expect(text).toMatch(/shown\s+once/i);
	});

	it('Connection says the settings are this browser’s, not the server’s', async () => {
		setPage('/ui/settings');
		render(SettingsPage);
		expect(panel().textContent).toMatch(/stored in this browser only/i);
		expect(panel().textContent).toMatch(/state of each part it depends on/i);
	});

	it('Proxmox says where the tokens go and what Save does', async () => {
		setPage('/ui/settings?tab=proxmox');
		render(SettingsPage);
		await waitFor(() => expect(api.getProxmoxSettings).toHaveBeenCalled());
		const text = panel().textContent ?? '';
		expect(text).toMatch(/vault and are never shown again/i);
		expect(text).toMatch(/blank to keep the one already stored/i);
	});
});

describe('F7: an env-locked setting says how to hand control to the UI', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue({
			timeout_seconds: 2,
			counts: { ok: 0, off: 1, unreachable: 0, unknown: 0 },
			subsystems: [
				{
					name: 'embeddings',
					configured: true,
					state: 'ok',
					target: 'http://embed.internal:8080/embed',
					consequence: 'KB search is ranked by the embedding service.',
				},
			],
		});
		listSettingOverrides.mockResolvedValue({
			settings: [
				{
					key: 'embedding_service_url',
					value: 'http://embed.internal:8080/embed',
					source: 'env',
					type: 'str',
					hot_reloadable: true,
					description: 'Embedding service KB search ranks with.',
					env_var: 'HP_EMBEDDING_SERVICE_URL',
					editable: false,
				},
			],
		});
	});

	it('names the variable AND the way to stop it deciding', async () => {
		render(SettingsPage);
		const field = await waitFor(() => {
			const el = document.querySelector('[data-setting="embedding_service_url"]');
			if (!el) throw new Error('not yet');
			return el;
		});
		const text = field.textContent ?? '';
		// Naming the variable explains the lock. The rest explains the exit -
		// without it, read-only reads as "not configurable".
		expect(text).toContain('HP_EMBEDDING_SERVICE_URL');
		expect(text).toMatch(/remove HP_EMBEDDING_SERVICE_URL from the server's environment/i);
		expect(text).toMatch(/\(\.env\) and restart/i);
		// Still read-only: the sentence is the way out, not an invitation to type.
		expect(field.querySelector('input')).toBeNull();
	});
});
