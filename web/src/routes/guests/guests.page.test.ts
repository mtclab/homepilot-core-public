import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import GuestsPage from './+page.svelte';
import { api, type Friend, type GuestFriends } from '$lib/api';
import { budgetCheck, earlierSummary, foldedInvites } from '$lib/guests';
import { inWords } from '$lib/relativeTime';

/**
 * The Guests page as people (3.12).
 *
 * Owner on prod 3.11.0: "Guests is a bit too simple at the moment, and
 * clunky" - two paragraphs, two raw tables, two always-open forms, and nothing
 * showing a friend's machine, its state or a way to its host page. Each case
 * asserts what the operator SEES and can DO, from one read.
 *
 * Teeth (each proven by reverting the code it names, see the PR):
 *  - the machine link's href          -> "links to its host page" fails;
 *  - the used-up sentence             -> "says so when the budget is used up";
 *  - folding (show all)               -> "folds the history" fails;
 *  - the Revoke confirm wiring        -> "revokes an open invite" fails;
 *  - the row's pre-filled CN          -> "Invite on a row pre-fills" fails;
 *  - the budget check line            -> "says the budget would refuse" fails;
 *  - dropping the token on close      -> "the link is gone after Done" fails;
 *  - the clipboard fallback           -> "selects the link" fails;
 *  - blank = null in the budget body  -> "a blank axis is no limit" fails.
 */

const getGuestFriends = api.getGuestFriends as ReturnType<typeof vi.fn>;
const mintGuestInvite = api.mintGuestInvite as ReturnType<typeof vi.fn>;
const setGuestQuota = api.setGuestQuota as ReturnType<typeof vi.fn>;
const deleteGuestQuota = api.deleteGuestQuota as ReturnType<typeof vi.fn>;
const revokeGuestInvite = api.revokeGuestInvite as ReturnType<typeof vi.fn>;

const DAY = 24 * 3600 * 1000;
const at = (offsetMs: number) => new Date(Date.now() + offsetMs).toISOString().replace(/\.\d{3}Z$/, 'Z');
const ISO = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function inv(id: string, state: string, extra: Partial<Friend['invites'][number]> = {}): Friend['invites'][number] {
	return {
		id,
		prefix: `hpi_${id.padEnd(12, '0')}`,
		state,
		caps: { template_vmid: 9000, node: 'pve1', cores: 1, memory_mb: 1024, disk_gb: 30 },
		created_at: at(-90 * DAY),
		expires_at: at(-83 * DAY),
		redeemed_at: null,
		revoked_at: null,
		host_id: null,
		...extra,
	};
}

/** Mika: one running machine, a budget that is used up, a long history. */
function mika(): Friend {
	return {
		cn: 'mika-260115',
		machines: [
			{ host_id: 'h-mika-web', name: 'mika-web', vmid: 130, node: 'pve1', state: 'running', cores: 1, memory_mb: 1024, disk_gb: 30 },
		],
		usage: { vms: 1, cores: 1, memory_mb: 1024, disk_gb: 30 },
		limits: { vms: 1, cores: null, memory_mb: 1024, disk_gb: 30 },
		invites: [
			inv('0ad8', 'redeemed', { redeemed_at: at(-60 * DAY), host_id: 'h-mika-web' }),
			inv('1aaa', 'redeemed', { redeemed_at: at(-120 * DAY) }),
			inv('1bbb', 'redeemed', { redeemed_at: at(-150 * DAY) }),
			inv('1ccc', 'expired', { expires_at: at(-200 * DAY) }),
		],
	};
}

/** Aino: no machine, no budget, one open invite. */
function aino(): Friend {
	return {
		cn: 'aino',
		machines: [],
		usage: { vms: 0, cores: 0, memory_mb: 0, disk_gb: 0 },
		limits: null,
		invites: [inv('2fbe', 'open', { created_at: at(-2 * DAY), expires_at: at(5 * DAY + 3600 * 1000) })],
	};
}

function payload(friends: Friend[], extra: Partial<GuestFriends> = {}): GuestFriends {
	return {
		friends,
		sizing: {
			presets: [
				{ name: 'small', cores: 1, memory_mb: 1024, disk_gb: 20 },
				{ name: 'medium', cores: 2, memory_mb: 2048, disk_gb: 20 },
			],
			bounds: { cores: { min: 1, max: 32 }, memory_mb: { min: 256, max: 65536 }, disk_gb: { min: 1, max: 2000 } },
			ttl_days: { min: 1, max: 90, default: 7 },
		},
		defaults: { node: 'pve1', template_vmid: 9000 },
		portal_origin: 'https://guests.example.com',
		...extra,
	};
}

function row(cn: string): HTMLElement {
	const el = document.querySelector(`[data-friend="${cn}"]`);
	if (!(el instanceof HTMLElement)) throw new Error(`no row for ${cn}`);
	return el;
}

async function rendered(friends: Friend[] = [mika(), aino()], extra: Partial<GuestFriends> = {}) {
	getGuestFriends.mockResolvedValue(payload(friends, extra));
	render(GuestsPage);
	await waitFor(() => expect(getGuestFriends).toHaveBeenCalled());
	if (friends.length) await screen.findByRole('list', { name: 'Friends' });
}

beforeEach(() => {
	vi.clearAllMocks();
	setPage('/ui/guests');
});
afterEach(() => cleanup());

describe('Guests: one row per friend', () => {
	it("shows a friend's machine with its live state, size and a link to its host page", async () => {
		await rendered();
		const r = row('mika-260115');
		const link = within(r).getByRole('link', { name: 'mika-web' });
		expect(link.getAttribute('href')).toBe('/ui/hosts/h-mika-web');
		const machine = r.querySelector('[data-machine="mika-web"]') as HTMLElement;
		expect(within(machine).getByText('running')).toBeTruthy();
		expect(machine.querySelector('[data-lamp="ok"]')).toBeTruthy();
		expect(within(machine).getByText('1 core · 1 GB · 30 GB')).toBeTruthy();
	});

	it('a stopped machine reads stopped with a parked lamp, never green', async () => {
		const f = mika();
		f.machines[0].state = 'stopped';
		await rendered([f]);
		const machine = row('mika-260115').querySelector('[data-machine="mika-web"]') as HTMLElement;
		expect(within(machine).getByText('stopped')).toBeTruthy();
		expect(machine.querySelector('[data-lamp="half"]')).toBeTruthy();
		expect(machine.querySelector('[data-lamp="ok"]')).toBeNull();
	});

	it('shows the budget as bars on the axes that have a limit, and says which have none', async () => {
		await rendered();
		const r = row('mika-260115');
		expect(r.querySelector('[data-budget-axis="vms"]')?.textContent).toBe('1 of 1');
		expect(r.querySelector('[data-budget-axis="memory_mb"]')?.textContent).toBe('1 of 1 GB');
		expect(r.querySelector('[data-budget-axis="disk_gb"]')?.textContent).toBe('30 of 30 GB');
		expect(r.querySelector('[data-budget-axis="cores"]')).toBeNull();
		expect(r.textContent).toMatch(/no limit on cores/);
		const meters = within(r).getAllByRole('meter');
		expect(meters.map((m) => m.getAttribute('aria-valuenow'))).toEqual(['100', '100', '100']);
	});

	it('says so in a plain sentence when the budget is used up', async () => {
		await rendered();
		expect(row('mika-260115').querySelector('[data-used-up]')?.textContent).toBe(
			'The budget is used up (machines, memory, disk): mika-260115 cannot build another machine until you raise it.',
		);
		// A friend with room left gets no such sentence.
		expect(row('aino').querySelector('[data-used-up]')).toBeNull();
	});

	it('a friend with no machine and no budget says both in words', async () => {
		await rendered();
		const r = row('aino');
		expect(r.textContent).toMatch('No machine yet');
		expect(r.textContent).toMatch('No budget: nothing caps what they hold');
		expect(within(r).queryAllByRole('meter')).toEqual([]);
	});

	it('counts people and machines in the head, and offers Invite and Budget on every row', async () => {
		await rendered();
		expect(screen.getByText('2 people · 1 machine')).toBeTruthy();
		for (const cn of ['mika-260115', 'aino']) {
			expect(within(row(cn)).getByRole('button', { name: `Invite ${cn}` })).toBeTruthy();
			expect(within(row(cn)).getByRole('button', { name: `Budget for ${cn}` })).toBeTruthy();
		}
	});
});

describe('Guests: the invite history folds under the friend', () => {
	it('shows the latest in words with its prefix, and folds the rest behind show all', async () => {
		await rendered();
		const r = row('mika-260115');
		const list = within(r).getByRole('list', { name: 'Invites for mika-260115' });
		expect(within(list).getAllByRole('listitem')).toHaveLength(1);
		expect(list.textContent).toMatch('redeemed 2 months ago');
		expect(list.textContent).toMatch('hpi_0ad800000000');
		expect(r.textContent).toMatch('2 earlier redeemed, 1 expired');

		const toggle = within(r).getByRole('button', { name: 'show all' });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(toggle);
		expect(within(list).getAllByRole('listitem')).toHaveLength(4);
		expect(within(r).getByRole('button', { name: 'show fewer' }).getAttribute('aria-expanded')).toBe('true');
	});

	it('writes dates in words with the exact time on hover, never a raw ISO string', async () => {
		await rendered();
		await fireEvent.click(within(row('mika-260115')).getByRole('button', { name: 'show all' }));
		expect(document.querySelector('#guests-panel')?.textContent).not.toMatch(ISO);
		const times = Array.from(document.querySelectorAll('[data-friend] time'));
		expect(times.length).toBeGreaterThanOrEqual(5);
		for (const t of times) {
			expect(t.getAttribute('datetime')).toMatch(ISO);
			expect(t.getAttribute('title')).toBeTruthy();
			expect(t.getAttribute('title')).not.toBe(t.getAttribute('datetime'));
		}
		expect(row('aino').textContent).toMatch('open, expires in 5 days');
	});

	it('never folds away an open invite, and offers Revoke only on open ones', async () => {
		const f = mika();
		f.invites.splice(2, 0, inv('9open', 'open', { created_at: at(-1 * DAY), expires_at: at(6 * DAY) }));
		await rendered([f]);
		const list = within(row('mika-260115')).getByRole('list', { name: 'Invites for mika-260115' });
		const shown = within(list).getAllByRole('listitem').map((li) => li.getAttribute('data-state'));
		expect(shown).toEqual(['redeemed', 'open']);
		expect(within(list).getAllByRole('button', { name: /^Revoke invite/ })).toHaveLength(1);
	});

	it('revokes an open invite after a confirm, and re-reads the friends', async () => {
		revokeGuestInvite.mockResolvedValue({ prefix: 'hpi_2fbe00000000', revoked: true });
		await rendered();
		await userEvent.click(within(row('aino')).getByRole('button', { name: 'Revoke invite hpi_2fbe00000000' }));
		const dlg = await screen.findByTestId('confirm-dialog');
		expect(dlg.textContent).toMatch('Revoke invite hpi_2fbe00000000 for aino');
		expect(dlg.textContent).toMatch(/stops working at once/);
		expect(revokeGuestInvite).not.toHaveBeenCalled();
		await userEvent.click(within(dlg).getByRole('button', { name: 'Revoke invite' }));
		await waitFor(() => expect(revokeGuestInvite).toHaveBeenCalledWith('hpi_2fbe00000000'));
		await waitFor(() => expect(getGuestFriends).toHaveBeenCalledTimes(2));
	});

	it('the folding rules, as pure functions', () => {
		const f = mika();
		const { shown, rest } = foldedInvites(f.invites);
		expect(shown.map((i) => i.id)).toEqual(['0ad8']);
		expect(earlierSummary(rest)).toBe('2 earlier redeemed, 1 expired');
		const now = new Date('2026-11-13T12:00:00Z');
		expect(inWords('2026-09-13T12:00:00Z', now)).toBe('2 months ago');
		expect(inWords('2026-11-18T13:00:00Z', now)).toBe('in 5 days');
		expect(inWords('2026-11-13T11:59:50Z', now)).toBe('just now');
	});
});

describe('Guests: Invite a friend', () => {
	it('opens from the page with an empty name, and from a row pre-filled with that CN', async () => {
		await rendered();
		await userEvent.click(screen.getByRole('button', { name: 'Invite a friend' }));
		let dlg = await screen.findByTestId('invite-dialog');
		expect((within(dlg).getByLabelText('Certificate name') as HTMLInputElement).value).toBe('');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Cancel' }));
		await waitFor(() => expect(screen.queryByTestId('invite-dialog')).toBeNull());

		await userEvent.click(within(row('aino')).getByRole('button', { name: 'Invite aino' }));
		dlg = await screen.findByTestId('invite-dialog');
		expect((within(dlg).getByLabelText('Certificate name') as HTMLInputElement).value).toBe('aino');
	});

	it('offers the sizes as presets, and Custom reveals cores, memory and disk', async () => {
		await rendered();
		await userEvent.click(screen.getByRole('button', { name: 'Invite a friend' }));
		const dlg = await screen.findByTestId('invite-dialog');
		const small = within(dlg).getByRole('button', { name: 'Small · 1 core · 1 GB · 20 GB' });
		const medium = within(dlg).getByRole('button', { name: 'Medium · 2 cores · 2 GB · 20 GB' });
		expect(small.getAttribute('aria-pressed')).toBe('true');
		expect(within(dlg).queryByLabelText('Cores')).toBeNull();
		await userEvent.click(medium);
		expect(medium.getAttribute('aria-pressed')).toBe('true');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Custom' }));
		// Custom starts from the size that was picked.
		expect((within(dlg).getByLabelText('Cores') as HTMLInputElement).value).toBe('2');
		expect((within(dlg).getByLabelText('Memory (GB)') as HTMLInputElement).value).toBe('2');
		expect((within(dlg).getByLabelText('Disk (GB)') as HTMLInputElement).value).toBe('20');
		// Template and node stay folded while the instance has defaults for both.
		expect(within(dlg).queryByLabelText('Node')).toBeNull();
		await userEvent.click(within(dlg).getByRole('button', { name: 'More options (template, node)' }));
		expect(within(dlg).getByLabelText('Node')).toBeTruthy();
	});

	it('says the budget would refuse it, before a link is sent - and nothing about the node', async () => {
		await rendered();
		await userEvent.click(within(row('mika-260115')).getByRole('button', { name: 'Invite mika-260115' }));
		const dlg = await screen.findByTestId('invite-dialog');
		const line = dlg.querySelector('[data-budget-check]') as HTMLElement;
		expect(line.getAttribute('data-budget-check')).toBe('refused');
		expect(line.textContent).toMatch(/mika-260115's budget would refuse this/);
		expect(line.textContent).toMatch('machines 1 of 1 used');
		// HomePilot has no read of a node's free capacity: the dialog does not invent one.
		expect(dlg.textContent).not.toMatch(/free|node has/i);

		await userEvent.clear(within(dlg).getByLabelText('Certificate name'));
		await userEvent.type(within(dlg).getByLabelText('Certificate name'), 'aino');
		expect(dlg.querySelector('[data-budget-check]')?.textContent).toBe('No budget for aino: nothing caps what they hold.');
	});

	it('the budget check is check_provision, axis for axis', () => {
		const f = mika();
		f.limits = { vms: 3, cores: 2, memory_mb: 4096, disk_gb: 60 };
		expect(budgetCheck(f, f.cn, { cores: 1, memory_mb: 1024, disk_gb: 30 }).fits).toBe(true);
		expect(budgetCheck(f, f.cn, { cores: 2, memory_mb: 1024, disk_gb: 30 }).text).toMatch('cores 1 of 2 used');
		expect(budgetCheck(f, f.cn, { cores: 1, memory_mb: 1024, disk_gb: 31 }).text).toMatch('disk 30 of 60 GB used');
	});

	it('mints, shows the link ONCE with a copy button, and the invite then appears under the friend', async () => {
		const TOKEN = 'hpi_0123456789ab' + 'cdef'.repeat(8);
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
		mintGuestInvite.mockResolvedValue({
			id: 'new1',
			token: TOKEN,
			cn: 'aino',
			caps: { node: 'pve1', template_vmid: 9000, pool: null, ipconfig0: null },
		});
		await rendered();
		// What the server answers after the mint: the invite, by prefix only.
		const after = aino();
		after.invites.unshift(inv('new1', 'open', { prefix: TOKEN.slice(0, 16), created_at: at(0), expires_at: at(7 * DAY) }));
		getGuestFriends.mockResolvedValue(payload([mika(), after]));

		await userEvent.click(within(row('aino')).getByRole('button', { name: 'Invite aino' }));
		const dlg = await screen.findByTestId('invite-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Create invite link' }));

		await waitFor(() => expect(mintGuestInvite).toHaveBeenCalled());
		expect(mintGuestInvite.mock.calls[0][0]).toEqual({
			cn: 'aino',
			template_vmid: null,
			node: null,
			cores: 1,
			memory_mb: 1024,
			disk_gb: 20,
			ttl_days: 7,
		});
		const panel = await waitFor(() => {
			const el = document.querySelector('[data-invite-link-panel]');
			if (!el) throw new Error('not yet');
			return el as HTMLElement;
		});
		const field = within(panel).getByLabelText('Invite link for aino') as HTMLInputElement;
		expect(field.value).toBe(`https://guests.example.com/invite/${TOKEN}`);
		expect(panel.textContent).toMatch(/only time the link is shown/i);
		expect(panel.textContent).toMatch(/keeps only a prefix and a hash/i);
		await userEvent.click(within(panel).getByRole('button', { name: 'Copy link' }));
		expect(writeText).toHaveBeenCalledWith(`https://guests.example.com/invite/${TOKEN}`);
		expect(within(panel).getByRole('button', { name: 'Copied' })).toBeTruthy();

		// The page re-read the friends: the new invite is under aino, by prefix.
		await waitFor(() => expect(getGuestFriends).toHaveBeenCalledTimes(2));
		await waitFor(() => expect(row('aino').querySelector(`[data-invite="${TOKEN.slice(0, 16)}"]`)).toBeTruthy());

		// Done: the link is gone from the page and was never written anywhere.
		await userEvent.click(within(panel).getByRole('button', { name: 'Done' }));
		await waitFor(() => expect(screen.queryByTestId('invite-dialog')).toBeNull());
		expect(document.body.innerHTML).not.toContain(TOKEN);
		expect(document.body.innerHTML).not.toContain(TOKEN.slice(16));
		expect(window.location.href).not.toContain(TOKEN.slice(16));
		for (let i = 0; i < localStorage.length; i++) {
			const k = localStorage.key(i) ?? '';
			expect(`${k}=${localStorage.getItem(k)}`).not.toContain(TOKEN.slice(16));
		}
		for (let i = 0; i < sessionStorage.length; i++) {
			const k = sessionStorage.key(i) ?? '';
			expect(`${k}=${sessionStorage.getItem(k)}`).not.toContain(TOKEN.slice(16));
		}
	});

	it('when copying is refused, selects the link so one keystroke copies it', async () => {
		Object.defineProperty(navigator, 'clipboard', {
			value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
			configurable: true,
		});
		mintGuestInvite.mockResolvedValue({ id: 'n2', token: 'hpi_feedfeedfeed' + 'ab'.repeat(16), cn: 'aino', caps: { node: 'pve1', template_vmid: 9000 } });
		await rendered([aino()], { portal_origin: null });
		await userEvent.click(within(row('aino')).getByRole('button', { name: 'Invite aino' }));
		const dlg = await screen.findByTestId('invite-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Create invite link' }));
		const panel = await screen.findByText(/only time the link is shown/i);
		const box = panel.closest('[data-invite-link-panel]') as HTMLElement;
		const field = within(box).getByLabelText('Invite link for aino') as HTMLInputElement;
		// No portal origin configured: the path, and a sentence saying so.
		expect(field.value).toMatch(/^\/invite\/hpi_feedfeedfeed/);
		expect(box.textContent).toMatch(/HP_PORTAL_BASE_URL/);
		await userEvent.click(within(box).getByRole('button', { name: 'Copy link' }));
		await waitFor(() => expect(within(box).getByRole('status').textContent).toMatch(/selected/));
		expect(document.activeElement).toBe(field);
		expect(field.selectionStart).toBe(0);
		expect(field.selectionEnd).toBe(field.value.length);
	});

	it('a refused mint says why inside the dialog and shows no link', async () => {
		mintGuestInvite.mockRejectedValue(new Error('Invalid caps: disk_gb too large'));
		await rendered();
		await userEvent.click(within(row('aino')).getByRole('button', { name: 'Invite aino' }));
		const dlg = await screen.findByTestId('invite-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Create invite link' }));
		expect((await within(dlg).findByRole('alert')).textContent).toMatch('Invalid caps');
		expect(document.querySelector('[data-invite-link-panel]')).toBeNull();
	});
});

describe('Guests: Budget', () => {
	it('shows current use beside each axis and the one-line consequence', async () => {
		await rendered();
		await userEvent.click(within(row('mika-260115')).getByRole('button', { name: 'Budget for mika-260115' }));
		const dlg = await screen.findByTestId('budget-dialog');
		expect((within(dlg).getByLabelText('Machines') as HTMLInputElement).value).toBe('1');
		expect((within(dlg).getByLabelText('Cores') as HTMLInputElement).value).toBe('');
		expect((within(dlg).getByLabelText('Memory (GB)') as HTMLInputElement).value).toBe('1');
		expect(dlg.textContent).toMatch('uses 1 GB');
		expect(dlg.textContent).toMatch('uses 30 GB');
		expect(dlg.textContent).toMatch('Lowering a budget never touches machines that already exist');
	});

	it('saves blank as no limit and GB as MB, then re-reads the bars', async () => {
		setGuestQuota.mockResolvedValue({});
		await rendered();
		await userEvent.click(within(row('mika-260115')).getByRole('button', { name: 'Budget for mika-260115' }));
		const dlg = await screen.findByTestId('budget-dialog');
		await userEvent.clear(within(dlg).getByLabelText('Machines'));
		await userEvent.type(within(dlg).getByLabelText('Machines'), '2');
		await userEvent.clear(within(dlg).getByLabelText('Memory (GB)'));
		await userEvent.type(within(dlg).getByLabelText('Memory (GB)'), '2.5');
		await userEvent.clear(within(dlg).getByLabelText('Disk (GB)'));
		await userEvent.click(within(dlg).getByRole('button', { name: 'Save budget' }));
		await waitFor(() =>
			expect(setGuestQuota).toHaveBeenCalledWith({
				cn: 'mika-260115',
				max_vms: 2,
				max_cores: null,
				max_memory_mb: 2560,
				max_disk_gb: null,
			}),
		);
		await waitFor(() => expect(screen.queryByTestId('budget-dialog')).toBeNull());
		expect(getGuestFriends).toHaveBeenCalledTimes(2);
	});

	it('leaves a memory budget it did not touch exactly as stored', async () => {
		setGuestQuota.mockResolvedValue({});
		const f = mika();
		f.limits = { vms: 1, cores: null, memory_mb: 1500, disk_gb: 30 };
		await rendered([f]);
		await userEvent.click(within(row('mika-260115')).getByRole('button', { name: 'Budget for mika-260115' }));
		const dlg = await screen.findByTestId('budget-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Save budget' }));
		await waitFor(() => expect(setGuestQuota).toHaveBeenCalled());
		expect(setGuestQuota.mock.calls[0][0].max_memory_mb).toBe(1500);
	});

	it('removes a budget with its own action, not four blanks', async () => {
		deleteGuestQuota.mockResolvedValue({ cn: 'mika-260115', limits: null });
		await rendered();
		await userEvent.click(within(row('mika-260115')).getByRole('button', { name: 'Budget for mika-260115' }));
		const dlg = await screen.findByTestId('budget-dialog');
		await userEvent.click(within(dlg).getByRole('button', { name: 'Remove budget' }));
		await waitFor(() => expect(deleteGuestQuota).toHaveBeenCalledWith('mika-260115'));
		expect(setGuestQuota).not.toHaveBeenCalled();
	});
});

describe('Guests: no friends yet', () => {
	it('says what a friend gets in one sentence, offers Invite, and keeps the long story folded', async () => {
		await rendered([]);
		const empty = await waitFor(() => {
			const el = document.querySelector('[data-guests-empty]');
			if (!el) throw new Error('not yet');
			return el as HTMLElement;
		});
		expect(empty.textContent).toMatch(/No friends yet\. A friend gets their own machine here/);
		expect(within(empty).getByRole('button', { name: 'Invite a friend' })).toBeTruthy();
		// One Invite button, not a second one in the title bar over an empty page.
		expect(screen.getAllByRole('button', { name: 'Invite a friend' })).toHaveLength(1);
		const details = screen.getByText('How the guest portal works').closest('details') as HTMLDetailsElement;
		expect(details.open).toBe(false);
		// No tables, no always-open forms.
		expect(document.querySelector('table')).toBeNull();
		expect(document.querySelectorAll('#guests-panel input')).toHaveLength(0);
		expect(screen.queryByRole('list', { name: 'Friends' })).toBeNull();
	});
});

describe('Guests: the phone card layout', () => {
	it('each friend is its own card: name, machines, budget, then wrapping actions, no table', async () => {
		await rendered();
		const list = screen.getByRole('list', { name: 'Friends' });
		const cards = Array.from(list.children);
		expect(cards.map((c) => c.getAttribute('data-friend'))).toEqual(['mika-260115', 'aino']);
		for (const card of cards) {
			// Reading order is the stacking order on a phone.
			const parts = Array.from(card.children).map((c) => c.className.split(' ')[0]);
			expect(parts).toEqual(['who', 'machines', 'budget', 'acts', 'hist']);
			const acts = card.querySelector('.acts') as HTMLElement;
			expect(acts.querySelectorAll('button')).toHaveLength(2);
		}
		expect(document.querySelector('table')).toBeNull();
	});
});
