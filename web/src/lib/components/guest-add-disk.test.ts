import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '../test-mocks';
import { goto } from '$app/navigation';
import GuestAddDisk from './GuestAddDisk.svelte';
import DiskPlan from './DiskPlan.svelte';
import * as apiModule from '$lib/api';
import { ApiError, type DiskOptions } from '$lib/api';

/**
 * #824: what an operator SEES adding a disk from a VM's page.
 *
 * - the form proposes ONE change with what was typed (SSD -> discard in the
 *   mount options) and opens that change's review - it never attaches anything;
 * - a guest the apply would refuse (disk hotplug off) shows why and cannot be
 *   proposed;
 * - a physical disk refusal shows the server's sentence with the root command;
 * - the review shows the slot, serial, fstab line and the blockers.
 *
 * Teeth (each verified by reverting the behaviour):
 *   - drop `blocked` from the Propose button's `disabled` and the hotplug case fails;
 *   - drop the `ssd ? ssd_mount_options` switch and the SSD case fails.
 */
const getDiskOptions = apiModule.getDiskOptions as unknown as ReturnType<typeof vi.fn>;
const proposeDisk = apiModule.proposeDisk as unknown as ReturnType<typeof vi.fn>;
const planDiskChange = apiModule.planDiskChange as unknown as ReturnType<typeof vi.fn>;

function opts(over: Partial<DiskOptions> = {}): DiskOptions {
	return {
		host_id: 'h1',
		hostname: 'app-01',
		vmid: 101,
		node: 'pve1',
		status: 'running',
		hotplug_disk: true,
		agent_enabled: true,
		agent_answers: true,
		next_slot: { scsi: 'scsi1', virtio: 'virtio0' },
		storages: [
			{ storage: 'fast-pool', type: 'lvmthin', active: true, shared: false, avail: 400 * 1024 ** 3, total: 900 * 1024 ** 3 },
		],
		filesystems: ['ext4', 'xfs'],
		label_max: { ext4: 16, xfs: 12 },
		size_gib: { min: 1, max: 65536 },
		default_mount_options: 'defaults,nofail',
		ssd_mount_options: 'defaults,nofail,discard',
		blockers: [],
		...over,
	};
}

describe('GuestAddDisk (#824)', () => {
	afterEach(() => cleanup());
	beforeEach(() => vi.clearAllMocks());

	it('proposes the change with SSD discard and opens its review', async () => {
		getDiskOptions.mockResolvedValue(opts());
		proposeDisk.mockResolvedValue({ id: '2026-11-18-add-disk-app-01-abc123', slot: 'scsi1' });
		render(GuestAddDisk, { hostId: 'h1' });

		await fireEvent.click(screen.getByRole('button', { name: 'Add a disk' }));
		await screen.findByText('scsi1');
		await fireEvent.input(screen.getByLabelText(/Size/), { target: { value: '50' } });
		await fireEvent.click(screen.getByLabelText(/on SSDs/));
		await fireEvent.click(screen.getByRole('button', { name: 'Propose the change' }));

		await waitFor(() => expect(proposeDisk).toHaveBeenCalledTimes(1));
		expect(proposeDisk).toHaveBeenCalledWith('h1', {
			storage: 'fast-pool',
			size_gib: 50,
			filesystem: 'ext4',
			mount_point: '/srv/data',
			label: 'data',
			mount_options: 'defaults,nofail,discard',
			bus: 'scsi',
			ssd: true,
		});
		await waitFor(() =>
			expect(goto).toHaveBeenCalledWith('/ui/changes/2026-11-18-add-disk-app-01-abc123'),
		);
	});

	it('shows why a guest without disk hotplug cannot get a disk, and does not propose', async () => {
		getDiskOptions.mockResolvedValue(
			opts({
				hotplug_disk: false,
				blockers: ['disk hotplug is off for this VM (Options > Hotplug > Disk)'],
			}),
		);
		render(GuestAddDisk, { hostId: 'h1' });
		await fireEvent.click(screen.getByRole('button', { name: 'Add a disk' }));

		expect(await screen.findByText(/disk hotplug is off/)).toBeTruthy();
		const propose = screen.getByRole('button', { name: 'Propose the change' }) as HTMLButtonElement;
		expect(propose.disabled).toBe(true);
		await fireEvent.click(propose);
		expect(proposeDisk).not.toHaveBeenCalled();
	});

	it("shows the server's refusal with the root command for a physical disk", async () => {
		getDiskOptions.mockResolvedValue(opts());
		proposeDisk.mockRejectedValue(
			new ApiError(
				422,
				JSON.stringify({
					detail: {
						code: 'raw_disk',
						message:
							'/dev/disk/by-id/ata-X is a physical disk. As root on node pve1, run: qm set 101 --scsi1 /dev/disk/by-id/ata-X,serial=hp1',
					},
				}),
			),
		);
		render(GuestAddDisk, { hostId: 'h1' });
		await fireEvent.click(screen.getByRole('button', { name: 'Add a disk' }));
		await screen.findByText('scsi1');
		await fireEvent.click(screen.getByRole('button', { name: 'Propose the change' }));

		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toContain('qm set 101 --scsi1 /dev/disk/by-id/ata-X');
		expect(goto).not.toHaveBeenCalled();
	});
});

describe('DiskPlan (#824)', () => {
	afterEach(() => cleanup());

	it('shows the slot, serial, fstab line and what would refuse it now', async () => {
		planDiskChange.mockResolvedValue({
			summary: 'Applying now would be refused, changing nothing: the guest is stopped',
			disk: {
				slot: 'scsi2',
				bus: 'scsi',
				storage: 'fast-pool',
				size_gib: 50,
				filesystem: 'ext4',
				label: 'appdata',
				mount_point: '/srv/data',
				mount_options: 'defaults,nofail',
				serial: 'hp0123456789ab',
				fstab_line: 'LABEL=appdata /srv/data ext4 defaults,nofail 0 2',
				hotplug_disk: true,
				agent: false,
				guest_steps: ['find the disk by its serial', 'mount it'],
				blockers: ['the guest is stopped'],
			},
		});
		render(DiskPlan, { artifactId: 'a1' });

		expect((await screen.findByTestId('disk-plan-slot')).textContent).toBe('scsi2');
		expect(screen.getByText('hp0123456789ab')).toBeTruthy();
		expect(screen.getByText('LABEL=appdata /srv/data ext4 defaults,nofail 0 2')).toBeTruthy();
		expect(screen.getByTestId('disk-plan-blockers').textContent).toContain('the guest is stopped');
	});
});
