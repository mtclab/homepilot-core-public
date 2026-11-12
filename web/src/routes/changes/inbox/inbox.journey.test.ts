import { render, screen, waitFor, fireEvent, within } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Inbox from './+page.svelte';
import { api } from '$lib/api';
import { hhmm } from '$lib/changes';

/**
 * The Inbox journeys (owner feedback 2026-11-08, #796, #797).
 *
 * The owner approved a change, it left the review queue, and nothing said it
 * still had to be applied - the empty queue read as "lost". These drive the
 * page against a small in-memory server (`world`) that moves a change through
 * its states the way the backend does, and assert what the owner SEES: the
 * card under "Ready to run", the "Running: <intent>, started HH:MM" line, the
 * outcome in words after it ran, a PVE warning explained rather than counted.
 *
 * Teeth (each proven by reverting the named code; evidence in the PR):
 *   - list only `proposed` in load() (the old queue) -> "approve only" fails:
 *     the approved change is on no list;
 *   - drop the poll (syncPoll) -> "approve and apply" never sees the outcome;
 *   - drop the typed confirmation in requestRun -> the destroy test fails;
 *   - type the summary's name (or a VMID) instead of the live read -> the
 *     "another machine" test offers Apply and fails (#813);
 *   - drop the cannot-run branch on the ready card -> the stale card shows
 *     Apply and the "will not run" test fails.
 */
const listArtifacts = api.listArtifacts as ReturnType<typeof vi.fn>;
const getArtifact = api.getArtifact as ReturnType<typeof vi.fn>;
const approveArtifact = api.approveArtifact as ReturnType<typeof vi.fn>;
const approveAndApply = api.approveAndApplyArtifact as ReturnType<typeof vi.fn>;
const withdrawArtifact = api.withdrawArtifact as ReturnType<typeof vi.fn>;
const applyArtifact = api.applyArtifact as ReturnType<typeof vi.fn>;
const getArtifactTarget = api.getArtifactTarget as ReturnType<typeof vi.fn>;
const recheckArtifact = api.recheckArtifact as ReturnType<typeof vi.fn>;

/** GET /artifacts/{id}/target: by default the recorded machine is there and the approval is fresh. */
function target(over: Record<string, unknown> = {}) {
	return {
		identity: { ok: true, code: null, message: null, guests: [] },
		approval: { approved_at: null, age_days: 0, limit_days: 7, stale: null },
		confirm: {
			vmid: 104,
			node: 'pve1',
			type: 'qemu',
			name: 'retire-104',
			status: 'running',
			cpus: 1,
			maxmem: 8 * 1024 ** 3,
			maxdisk: 30 * 1024 ** 3,
			owner: 'alice',
		},
		may_run: true,
		...over,
	};
}

const STARTED = new Date(2026, 10, 8, 18, 28).toISOString();
const EFI =
	"EFI disk without 'ms-cert=2023k' option, suggesting that not all UEFI 2023 certificates from Microsoft are enrolled yet.";

interface World {
	status: string;
	active: Record<string, unknown> | null;
	last: Record<string, unknown> | null;
	fmExtra: Record<string, unknown>;
	summary: Record<string, unknown>;
}

let world: World;

const ID = '2026-11-08-back-up-vm-104-aaaaaa';
const FM = {
	id: ID,
	kind: 'proxmox-api-sequence',
	intent: 'Back up VM 104',
	target: { host: 'retire-104', node: 'pve1' },
	created_at: '2026-11-08T17:00:00Z',
};

function artifact() {
	return { ...FM, status: world.status, ...world.fmExtra };
}

function backupSummary() {
	return {
		headline: 'Back up VM 104',
		target: 'retire-104 (VM 104 on pve1)',
		steps: ['Back up retire-104 (VM 104 on pve1) to fast1'],
		danger: null,
		confirm_name: null,
		two_step: false,
		two_step_reason: null,
	};
}

function serve() {
	listArtifacts.mockImplementation((params: { status?: string } = {}) =>
		Promise.resolve(
			params.status === world.status
				? { items: [artifact()], total: 1 }
				: { items: [], total: 0 },
		),
	);
	getArtifact.mockImplementation(() =>
		Promise.resolve({
			frontmatter: artifact(),
			body: 'raw spec text',
			active_task: world.active,
			approval_code: world.status === 'proposed' ? 'ABCDE-FGHJK' : null,
			approval_locked: false,
			summary: world.summary,
			last_task: world.last,
		}),
	);
}

function finishApplied(warnings: unknown[]) {
	const now = new Date().toISOString();
	world.status = 'applied';
	world.active = null;
	world.fmExtra = { applied_at: now };
	world.last = {
		id: 't1',
		artifact_id: ID,
		action: 'apply',
		status: 'succeeded',
		created_at: STARTED,
		finished_at: now,
		error: null,
		result: { warnings },
	};
}

describe('Changes > Inbox journeys', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ shouldAdvanceTime: true });
		world = { status: 'proposed', active: null, last: null, fmExtra: {}, summary: backupSummary() };
		serve();
		getArtifactTarget.mockImplementation(() => Promise.resolve(target()));
		approveAndApply.mockImplementation(() => {
			world.status = 'approved';
			world.fmExtra = { approved_by: { user: 'owner (hp_owner)' } };
			world.active = { id: 't1', status: 'running', action: 'apply', created_at: STARTED };
			return Promise.resolve({ task_id: 't1', artifact_id: ID, status: 'pending', action: 'apply' });
		});
		approveArtifact.mockImplementation(() => {
			world.status = 'approved';
			return Promise.resolve({ id: ID, status: 'approved' });
		});
		withdrawArtifact.mockImplementation(() => {
			world.status = 'withdrawn';
			world.fmExtra = {
				withdrawn_by: { user: 'owner (hp_owner)', at: new Date().toISOString() },
			};
			world.last = {
				id: 'tw',
				artifact_id: ID,
				action: 'withdraw',
				status: 'succeeded',
				created_at: STARTED,
				finished_at: new Date().toISOString(),
				error: null,
				result: { withdrawn: true },
			};
			return Promise.resolve({ id: ID, status: 'withdrawn', task_id: 'tw' });
		});
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('approve and apply: one action, then Running with its start time, then the outcome in words', async () => {
		render(Inbox);
		const card = await screen.findByTestId('review-card');
		// The change in plain words, before anyone decides.
		await within(card).findByText('Back up retire-104 (VM 104 on pve1) to fast1');

		await fireEvent.click(within(card).getByRole('button', { name: /approve and apply/i }));

		await waitFor(() => expect(approveAndApply).toHaveBeenCalledWith(ID));
		// Running: still in the Inbox, saying what runs and since when.
		const running = await screen.findByTestId('running-line');
		expect(running.textContent).toContain(`Running: Back up VM 104, started ${hhmm(STARTED)}`);
		expect(screen.queryByTestId('inbox-empty')).toBeNull();

		// The run ends; the live poll notices and the outcome is stated.
		finishApplied([{ text: EFI, explanation: 'Harmless for this task: ...', harmless: true }]);
		await vi.advanceTimersByTimeAsync(2000);

		const done = await screen.findByTestId('finished-card');
		await within(done).findByText(/^Done at \d\d:\d\d: Back up VM 104\.$/);
		const warnings = within(done).getByTestId('pve-warnings');
		expect(warnings.textContent).toContain('harmless');
		expect(warnings.textContent).toContain('ms-cert=2023k');
		expect(warnings.textContent).not.toMatch(/WARNINGS: 1/);
		expect(await screen.findByTestId('inbox-empty')).toBeInTheDocument();
	});

	it('approve only: the change stays in the Inbox as Ready to run, with Apply on its card', async () => {
		render(Inbox);
		const card = await screen.findByTestId('review-card');
		await within(card).findByRole('button', { name: /approve and apply/i });

		await fireEvent.click(within(card).getByRole('button', { name: /approve only/i }));

		const ready = await screen.findByTestId('ready-card');
		expect(within(ready).getByText('Ready to run')).toBeInTheDocument();
		expect(within(ready).getByRole('button', { name: 'Apply' })).toBeInTheDocument();
		expect(within(ready).getByRole('button', { name: 'Withdraw' })).toBeInTheDocument();
		expect(screen.queryByTestId('inbox-empty')).toBeNull();
		expect(screen.getByTestId('inbox-counts').textContent).toContain('1 ready to run');

		// And Apply is right there: it runs it.
		applyArtifact.mockImplementation(() => {
			world.active = { id: 't2', status: 'pending', action: 'apply', created_at: STARTED };
			return Promise.resolve({ task_id: 't2' });
		});
		await fireEvent.click(within(ready).getByRole('button', { name: 'Apply' }));
		await waitFor(() => expect(applyArtifact).toHaveBeenCalledWith(ID));
		expect((await screen.findByTestId('running-line')).textContent).toContain('task pending');
	});

	it('withdraw: an approved change is taken back, says it never ran, and the Inbox empties', async () => {
		world.status = 'approved';
		render(Inbox);
		const ready = await screen.findByTestId('ready-card');

		await fireEvent.click(within(ready).getByRole('button', { name: 'Withdraw' }));
		// Inline confirm: nothing has happened yet.
		expect(withdrawArtifact).not.toHaveBeenCalled();
		await fireEvent.click(within(ready).getByRole('button', { name: 'Confirm withdraw' }));

		await waitFor(() => expect(withdrawArtifact).toHaveBeenCalledWith(ID));
		const done = await screen.findByTestId('finished-card');
		await within(done).findByText(/Withdrawn by owner \(hp_owner\)\. It was never run\./);
		expect(await screen.findByTestId('inbox-empty')).toBeInTheDocument();
	});

	it('a destroy asks for the guest name typed back before it runs', async () => {
		world.summary = {
			...backupSummary(),
			steps: ['Destroy VM 104 on pve1, with its disks'],
			danger: 'destroy',
			confirm_name: 'retire-104',
		};
		render(Inbox);
		const card = await screen.findByTestId('review-card');
		await within(card).findByText('Destroy VM 104 on pve1, with its disks');

		await fireEvent.click(within(card).getByRole('button', { name: /approve and apply/i }));

		const dialog = await screen.findByTestId('confirm-dialog');
		expect(dialog.textContent).toContain('cannot be undone');
		const go = within(dialog).getByRole('button', { name: 'Approve and apply' });
		expect(go).toBeDisabled();
		expect(approveAndApply).not.toHaveBeenCalled();
		// The guest as it is now, beside the name asked for (#813).
		expect(within(dialog).getByTestId('confirm-live-state').textContent).toContain(
			'retire-104, running · 1 core · 8 GB RAM · 30 GB disk · owner alice',
		);
		await fireEvent.input(within(dialog).getByRole('textbox'), {
			target: { value: '104' },
		});
		expect(go).toBeDisabled();
		await fireEvent.input(within(dialog).getByRole('textbox'), {
			target: { value: 'retire-104' },
		});
		expect(go).not.toBeDisabled();
		await fireEvent.click(go);
		await waitFor(() => expect(approveAndApply).toHaveBeenCalledWith(ID));
	});

	it('a destroy whose VMID another machine holds now offers no Apply at all (#813)', async () => {
		world.status = 'approved';
		world.fmExtra = { approved_by: { user: 'owner (hp_owner)', at: new Date().toISOString() } };
		world.summary = {
			...backupSummary(),
			steps: ['Destroy VM 104 on pve1, with its disks'],
			danger: 'destroy',
			confirm_name: 'retire-104',
		};
		const changed =
			"VMID 104 is now 'alice-web' (VM, running, on pve1), not 'retire-104' - Proxmox reused the number or the guest was renamed";
		let reads = 0;
		getArtifactTarget.mockImplementation(() => {
			// The card's read finds the machine still there; by the time Apply is
			// pressed, PVE has given the number to someone else.
			reads += 1;
			return Promise.resolve(
				reads === 1
					? target()
					: target({
							identity: { ok: false, code: 'changed', message: changed, guests: [] },
							confirm: null,
							may_run: false,
						}),
			);
		});
		render(Inbox);
		const ready = await screen.findByTestId('ready-card');
		await waitFor(() => expect(getArtifactTarget).toHaveBeenCalledTimes(1));
		await fireEvent.click(await within(ready).findByRole('button', { name: 'Apply' }));

		const dialog = await screen.findByTestId('confirm-dialog');
		expect(within(dialog).getByRole('alert').textContent).toContain("'alice-web'");
		expect(within(dialog).queryByRole('textbox')).toBeNull();
		expect(within(dialog).queryByRole('button', { name: 'Apply' })).toBeNull();
		expect(applyArtifact).not.toHaveBeenCalled();
	});

	it('an approval that will not run says why, and its age, instead of offering Apply (#813)', async () => {
		const at = new Date(Date.now() - 74 * 24 * 3600 * 1000).toISOString();
		world.status = 'approved';
		world.fmExtra = { approved_by: { user: 'owner (hp_owner)', at } };
		const stale =
			'it was approved 74 days ago and approvals expire after 7 days (setting approval_max_age_days); the cluster may have changed since - review it again';
		getArtifactTarget.mockImplementation(() =>
			Promise.resolve(
				target({ approval: { approved_at: at, age_days: 74, limit_days: 7, stale }, may_run: false }),
			),
		);
		recheckArtifact.mockImplementation(() => {
			world.status = 'proposed';
			world.fmExtra = { returned_for_review: { code: 'stale', reason: stale } };
			return Promise.resolve({ id: ID, may_run: false, status: 'proposed' });
		});
		render(Inbox);
		const ready = await screen.findByTestId('ready-card');
		expect(within(ready).getByTestId('approval-age').textContent).toBe('approved 74 days ago');
		expect((await within(ready).findByTestId('cannot-run')).textContent).toContain('74 days ago');
		expect(screen.getByTestId('cannot-run-banner').textContent).toContain('1 approved change');
		expect(within(ready).queryByRole('button', { name: 'Apply' })).toBeNull();

		await fireEvent.click(within(ready).getByRole('button', { name: 'Back to review' }));

		await waitFor(() => expect(recheckArtifact).toHaveBeenCalledWith(ID));
		const card = await screen.findByTestId('review-card');
		expect(within(card).getByTestId('returned-for-review').textContent).toContain('74 days ago');
		expect(applyArtifact).not.toHaveBeenCalled();
	});

	it('a partly applied run names what happened, not just "failed" (#796)', async () => {
		world.status = 'failed';
		world.fmExtra = {
			failed_at: new Date().toISOString(),
			failure_reason:
				'partly applied: step strip changed scsi2 before it failed (not applied: hostpci0): 500',
			partial_apply: {
				steps_done: [],
				steps_failed: ['strip'],
				steps_not_run: [],
				error: '500',
				step_reread: 'strip',
				keys_applied: ['scsi2'],
				keys_not_applied: ['hostpci0'],
			},
		};
		render(Inbox);
		const done = await screen.findByTestId('finished-card');
		await within(done).findByText('Partly applied');
		expect(within(done).getByTestId('outcome-sentence').textContent).toMatch(
			/^Partly applied: step strip changed scsi2/,
		);
		expect(within(done).getByTestId('partial-apply').textContent).toContain(
			'Changed before the failure: scsi2',
		);
	});

	it('an unknown PVE warning is shown verbatim and marked not recognised', async () => {
		finishApplied([{ text: 'disk scsi1 skipped', explanation: null, harmless: null }]);
		render(Inbox);
		const done = await screen.findByTestId('finished-card');
		const warnings = await within(done).findByTestId('pve-warnings');
		expect(warnings.textContent).toContain('disk scsi1 skipped');
		expect(warnings.textContent).toContain('not recognised');
	});

	it('keyboard: j selects the card, a asks before approving and applying', async () => {
		render(Inbox);
		const card = await screen.findByTestId('review-card');
		await within(card).findByRole('button', { name: /approve and apply/i });

		await fireEvent.keyDown(window, { key: 'j' });
		expect(document.activeElement).toBe(card);
		await fireEvent.keyDown(card, { key: 'a' });

		const dialog = await screen.findByTestId('confirm-dialog');
		expect(approveAndApply).not.toHaveBeenCalled();
		await fireEvent.click(within(dialog).getByRole('button', { name: 'Approve and apply' }));
		await waitFor(() => expect(approveAndApply).toHaveBeenCalledWith(ID));
	});

	it('says the Inbox is empty only when nothing waits', async () => {
		world.status = 'applied';
		render(Inbox);
		expect(await screen.findByText('Inbox empty. Nothing is waiting for you.')).toBeInTheDocument();
	});
});
