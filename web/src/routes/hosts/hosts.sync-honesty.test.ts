import { render, cleanup } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import HostsPage from './+page.svelte';
import { api } from '$lib/api';
import { notify } from '$lib/stores';

/**
 * Sync, when the hypervisor sweep did not finish (#648 tranche 8).
 *
 * `POST /inventory/refresh` returns counts. It used to return the SAME shape
 * whether it had walked the whole hypervisor or died on the first node, so the
 * page raised a green "Synced: N hosts" either way and the operator read a
 * partial answer as the estate. The backend now says `complete`, and this is
 * the operator-facing half: a sweep that did not finish may not be reported as
 * one that did.
 *
 * Teeth: report `res.hosts` unconditionally and the incomplete cases fail on
 * the green toast; treat a missing `complete` as success and the older-backend
 * case fails.
 */
const refreshInventory = api.refreshInventory as ReturnType<typeof vi.fn>;
const enrichInventory = api.enrichInventory as ReturnType<typeof vi.fn>;
const listInventory = api.listInventory as ReturnType<typeof vi.fn>;
const notified = notify as unknown as ReturnType<typeof vi.fn>;

async function pressSync(): Promise<void> {
	const user = userEvent.setup();
	const { container } = render(HostsPage);
	const button = Array.from(container.querySelectorAll('button')).find((b) =>
		/sync/i.test(b.textContent ?? ''),
	);
	if (!button) throw new Error('no Sync button');
	await user.click(button);
}

/** Every message the page raised, and how it graded each one. */
function messages(): string {
	return notified.mock.calls.map((c) => `${c[1] ?? 'ok'}:${c[0]}`).join('\n');
}

describe('Hosts: a sync that did not finish is not a sync that did', () => {
	afterEach(() => cleanup());

	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/hosts');
		listInventory.mockResolvedValue({ items: [], total: 0 });
		enrichInventory.mockResolvedValue({ enriched: 0, failed: 0, skipped: 0, unchanged: 0 });
	});

	it('says so when the hypervisor sweep was incomplete', async () => {
		refreshInventory.mockResolvedValue({
			hosts: 2,
			services: 0,
			complete: false,
			error: 'pve is down',
		});

		await pressSync();

		expect(messages()).toMatch(/err:Sync did not finish/);
		expect(messages()).toMatch(/pve is down/);
		expect(messages()).not.toMatch(/ok:Synced/);
	});

	it('does not treat a backend that cannot say as a backend that said yes', async () => {
		refreshInventory.mockResolvedValue({ hosts: 2, services: 0 });

		await pressSync();

		expect(messages()).not.toMatch(/ok:Synced/);
	});

	it('still reports a real, finished sync plainly', async () => {
		// The honest success has to stay reachable, or every sync reads as a
		// fault and the operator learns to ignore the toast.
		refreshInventory.mockResolvedValue({ hosts: 7, services: 3, complete: true });

		await pressSync();

		expect(messages()).toMatch(/ok:Synced: 7 hosts, 3 services/);
		expect(messages()).not.toMatch(/did not finish/);
	});
});
