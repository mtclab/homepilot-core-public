import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../../lib/test-mocks';
import Detail from './+page.svelte';
import { api } from '$lib/api';

/**
 * #506: a proposed change that a structured policy rule matches says so on the
 * review screen, before anyone presses Approve - what matched, which policy, and
 * that approving needs the policy acknowledged. A rule that does not parse is
 * said, not swallowed.
 *
 * Teeth: drop <PolicyWarnings> from the change page and the first test fails;
 * drop `acknowledgePolicy` from the approve call and the checkbox test fails;
 * drop the `approves` guard and the disabled-until-ticked assertion fails.
 */
const getArtifact = api.getArtifact as ReturnType<typeof vi.fn>;
const approveArtifact = api.approveArtifact as ReturnType<typeof vi.fn>;
const approveAndApply = api.approveAndApplyArtifact as ReturnType<typeof vi.fn>;

const ID = '2026-11-18-destroy-web1-000001';

function proposed(policy_warnings: unknown[], status = 'proposed') {
	return {
		frontmatter: {
			id: ID,
			kind: 'proxmox-api-sequence',
			status,
			intent: 'Destroy web1',
			target: { host: 'web1' },
			created_at: '2026-11-18T10:00:00Z',
		},
		body: 'the plan',
		active_task: null,
		approval_code: null,
		policy_warnings,
	};
}

const MATCH = {
	policy_id: 7,
	policy_title: 'Never destroy a guest by API',
	applies_via: 'target',
	rule: 'forbid kind proxmox-api-sequence, verb DELETE, path /nodes/*/qemu/*',
	artifact_id: ID,
	matched: ['DELETE /nodes/pve1/qemu/101'],
};

describe('Review screen: policy warnings', () => {
	beforeEach(() => vi.clearAllMocks());

	it('says which policy the change matches, what matched, and that approving needs it acknowledged', async () => {
		getArtifact.mockResolvedValue(proposed([MATCH]));
		render(Detail);
		const panel = await screen.findByTestId('policy-warnings');
		expect(panel.textContent).toContain('This change matches a policy');
		expect(panel.textContent).toContain('Never destroy a guest by API');
		expect(panel.textContent).toContain('DELETE /nodes/pve1/qemu/101');
		expect(panel.textContent).toContain(`hp changes approve ${ID} --acknowledge-policy`);
	});

	it('holds Approve until the operator ticks the acknowledgement, then sends it', async () => {
		getArtifact.mockResolvedValue({ ...proposed([MATCH]), summary: { steps: ['Destroy web1'], two_step: true, danger: null } });
		approveArtifact.mockResolvedValue({ id: ID, status: 'approved' });
		render(Detail);
		const approve = await screen.findByRole('button', { name: 'Approve' });
		expect((approve as HTMLButtonElement).disabled).toBe(true);
		const ack = (await screen.findByTestId('policy-ack')).querySelector('input') as HTMLInputElement;
		await fireEvent.click(ack);
		expect((approve as HTMLButtonElement).disabled).toBe(false);
		await fireEvent.click(approve);
		await waitFor(() => expect(approveArtifact).toHaveBeenCalledWith(expect.any(String), { acknowledgePolicy: true }));
	});

	it('sends the acknowledgement with Approve and apply too', async () => {
		getArtifact.mockResolvedValue({ ...proposed([MATCH]), summary: { steps: ['Destroy web1'], two_step: false, danger: null } });
		approveAndApply.mockResolvedValue({ task_id: 't1', artifact_id: ID, status: 'pending', action: 'apply' });
		render(Detail);
		const ack = (await screen.findByTestId('policy-ack')).querySelector('input') as HTMLInputElement;
		await fireEvent.click(ack);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve and apply' }));
		await waitFor(() => expect(approveAndApply).toHaveBeenCalledWith(expect.any(String), { acknowledgePolicy: true }));
	});

	it('approves a change no rule matches without asking', async () => {
		getArtifact.mockResolvedValue({ ...proposed([]), summary: { steps: ['x'], two_step: true, danger: null } });
		approveArtifact.mockResolvedValue({ id: ID, status: 'approved' });
		render(Detail);
		const approve = await screen.findByRole('button', { name: 'Approve' });
		expect((approve as HTMLButtonElement).disabled).toBe(false);
		await fireEvent.click(approve);
		await waitFor(() => expect(approveArtifact).toHaveBeenCalledWith(expect.any(String), { acknowledgePolicy: false }));
	});

	it('says a rule it could not read, and shows nothing for a change no rule matches', async () => {
		getArtifact.mockResolvedValue(
			proposed([{ ...MATCH, rule: null, matched: [], errors: ['policy-rule block 1, entry 1 has unknown field(s)'] }]),
		);
		render(Detail);
		const err = await screen.findByTestId('policy-rule-error');
		expect(err.textContent).toContain('unknown field');
		expect(screen.queryByTestId('policy-warning')).toBeNull();
	});

	it('shows no warning once the change has left proposed', async () => {
		getArtifact.mockResolvedValue(proposed([MATCH], 'approved'));
		render(Detail);
		await screen.findByRole('heading', { name: 'Destroy web1' });
		expect(screen.queryByTestId('policy-warnings')).toBeNull();
	});
});
