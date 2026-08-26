import { render, screen, waitFor, cleanup } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
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
const getGuests = api.getGuests as ReturnType<typeof vi.fn>;
const mintGuestInvite = api.mintGuestInvite as ReturnType<typeof vi.fn>;
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const listSettingOverrides = api.listSettingOverrides as ReturnType<typeof vi.fn>;
const listTokens = api.listTokens as ReturnType<typeof vi.fn>;

/** The rendered settings panel, whichever tab is open. */
function panel(): HTMLElement {
	const el = document.getElementById('settings-panel');
	if (!(el instanceof HTMLElement)) throw new Error('no settings panel');
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

describe('F7: the Guests tab explains every value it asks for', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=guests');
		getGuests.mockResolvedValue({ guests: [], invites: [] });
	});

	it('leads with what the guest portal actually is', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());
		const text = panel().textContent ?? '';
		// A friend, a certificate, a machine, a budget: the four nouns an operator
		// needs before any of the boxes below mean anything.
		expect(text).toMatch(/client certificate/i);
		expect(text).toMatch(/guest portal/i);
		expect(text).toMatch(/budget caps the TOTAL/i);
	});

	it('gives EVERY input on the tab its own explanation', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());

		const controls = Array.from(panel().querySelectorAll('input, select, textarea'));
		// A tab that renders no inputs would pass a per-input walk vacuously.
		expect(controls.length).toBeGreaterThanOrEqual(10);

		const undescribed = controls.filter((c) => describedBy(c).length < 20).map(nameOf);
		expect(undescribed).toEqual([]);

		// And every input is labelled, so the description has something to describe.
		const unlabelled = controls.filter((c) => {
			const id = c.getAttribute('id');
			return !id || !document.querySelector(`label[for="${id}"]`);
		});
		expect(unlabelled.map(nameOf)).toEqual([]);
	});

	it('says that an empty node or template means the Provisioning default', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());

		for (const id of ['guest-invite-node', 'guest-invite-template']) {
			const input = document.getElementById(id);
			expect(input, id).toBeTruthy();
			const note = describedBy(input!);
			expect(note, id).toMatch(/leave it empty/i);
			expect(note, id).toMatch(/provisioning default/i);
		}
		// And the explanation points at the place those defaults are set, rather
		// than naming a page the operator then has to hunt for.
		const link = panel().querySelector('a[href="/ui/settings?tab=subsystems"]');
		expect(link?.textContent).toMatch(/provisioning defaults/i);
	});

	it('separates the per-machine caps from the friend-wide budget', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());

		expect(describedBy(document.getElementById('guest-invite-cores')!)).toMatch(
			/not on the friend/i,
		);
		expect(describedBy(document.getElementById('guest-quota-cores')!)).toMatch(
			/across every machine/i,
		);
		// Lowering a budget is the question an operator asks before typing a
		// smaller number, so the answer is on the form, not in the docs.
		expect(panel().textContent).toMatch(/never touches machines that already exist/i);
	});

	it('explains the invite TTL, and offers it as a field at all', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());
		const ttl = document.getElementById('guest-invite-ttl');
		expect(ttl).toBeTruthy();
		const note = describedBy(ttl!);
		expect(note).toMatch(/redeemable/i);
		expect(note).toMatch(/one-time/i);
	});

	it('mints with no node or template, and shows which defaults the invite got', async () => {
		mintGuestInvite.mockResolvedValue({
			id: 'inv1',
			token: 'abcd.secret',
			cn: 'friend.example',
			caps: { node: 'pve1', template_vmid: 9000, pool: null, ipconfig0: null },
		});
		render(SettingsPage);
		await waitFor(() => expect(getGuests).toHaveBeenCalled());

		// The GOAL: an operator who names a person and a size gets an invite. The
		// infra half is optional server-side (#553 C3) and the form must not
		// re-impose it.
		await userEvent.type(document.getElementById('guest-invite-cn')!, 'friend.example');
		await userEvent.click(screen.getByRole('button', { name: 'Mint' }));

		await waitFor(() => expect(mintGuestInvite).toHaveBeenCalled());
		expect(mintGuestInvite.mock.calls[0][0]).toMatchObject({
			cn: 'friend.example',
			node: null,
			template_vmid: null,
		});

		// The token once, and the resolved defaults next to it - the only place
		// "empty" is shown to have meant something concrete.
		const caps = await waitFor(() => {
			const el = panel().querySelector('[data-minted-caps]');
			if (!el) throw new Error('not yet');
			return el;
		});
		expect(caps.textContent).toMatch(/template 9000/);
		expect(caps.textContent).toMatch(/pve1/);
		expect(panel().textContent).toMatch(/shown once and stored nowhere/i);
	});
});

describe('F7: the other Settings sectors lead in before they ask', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('Monitoring says what a rule does and where an alert surfaces', async () => {
		setPage('/ui/settings?tab=monitoring');
		render(SettingsPage);
		await waitFor(() => expect(api.listAlertRules).toHaveBeenCalled());
		const text = panel().textContent ?? '';
		expect(text).toMatch(/held for the whole duration/i);
		expect(text).toMatch(/Overview and on the affected host/i);
		// And the compact rule row is explained as the sentence it is.
		const rowNote = panel().querySelector('[data-alert-form-note]');
		expect(rowNote?.textContent).toMatch(/fires when/i);
	});

	it('Tokens says what a token is for, the scope ladder, and the shown-once rule', async () => {
		setPage('/ui/settings?tab=tokens');
		listTokens.mockResolvedValue({ items: [], total: 0 });
		render(SettingsPage);
		await waitFor(() => expect(listTokens).toHaveBeenCalled());
		const text = panel().textContent ?? '';
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
