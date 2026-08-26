import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { api, ApiError } from '$lib/api';

/**
 * THE C2 GATE (#553): the non-secret settings are editable from the product,
 * on the card of the subsystem they configure, and every field says where its
 * value comes from.
 *
 * The defects this forbids:
 *  - an input offered for a setting the environment decides, so "Save" gets a
 *    409 the operator was given no way to predict;
 *  - a 409 collapsed into "Request failed", hiding WHICH variable overrides the
 *    setting and that nothing was recorded;
 *  - a secret rendered anywhere on this surface.
 *
 * Teeth: render the env-locked field as an input and the read-only case fails;
 * swallow the ApiError detail and the 409 case fails; drop the source labels
 * and the "says where the value comes from" case fails.
 */
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;
const listSettingOverrides = api.listSettingOverrides as ReturnType<typeof vi.fn>;
const saveSettingOverride = api.saveSettingOverride as ReturnType<typeof vi.fn>;
const clearSettingOverride = api.clearSettingOverride as ReturnType<typeof vi.fn>;

const REPORT = {
	timeout_seconds: 2,
	counts: { ok: 1, off: 1, unreachable: 0, unknown: 0 },
	subsystems: [
		{
			name: 'artifacts_remote',
			configured: true,
			state: 'ok',
			target: 'ssh://backup.example:22',
			consequence: 'Artifacts are pushed to ssh://backup.example:22 on a schedule.',
		},
		{
			name: 'embeddings',
			configured: false,
			state: 'off',
			target: '',
			consequence: 'KB search is keyword-only because no embedding service is configured.',
		},
	],
};

const OVERRIDES = [
	{
		key: 'artifacts_remote',
		value: 'ssh://backup.example:22/archive.git',
		source: 'db',
		type: 'str',
		hot_reloadable: true,
		description: 'Git remote the artifact store is pushed to.',
		env_var: 'HP_ARTIFACTS_REMOTE',
		editable: true,
	},
	{
		key: 'artifacts_push_interval_seconds',
		value: 3600,
		source: 'default',
		type: 'int',
		hot_reloadable: true,
		description: 'How often the artifact store is pushed to its remote.',
		env_var: 'HP_ARTIFACTS_PUSH_INTERVAL_SECONDS',
		editable: true,
	},
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
	{
		key: 'embedding_model',
		value: 'bge-m3',
		source: 'default',
		type: 'str',
		hot_reloadable: true,
		description: 'Model name asked of the embedding service.',
		env_var: 'HP_EMBEDDING_MODEL',
		editable: true,
	},
	{
		key: 'retention_days',
		value: 90,
		source: 'default',
		type: 'int',
		hot_reloadable: true,
		description: 'How long operational history is kept.',
		env_var: 'HP_RETENTION_DAYS',
		editable: true,
	},
	{
		key: 'metrics_retention_days',
		value: 7,
		source: 'default',
		type: 'int',
		hot_reloadable: true,
		description: 'How long raw metric samples are kept.',
		env_var: 'HP_METRICS_RETENTION_DAYS',
		editable: true,
	},
];

/** The field block for one setting key. */
function field(key: string): HTMLElement {
	const el = document.querySelector(`[data-setting="${key}"]`);
	if (!(el instanceof HTMLElement)) throw new Error(`no field for ${key}`);
	return el;
}

describe('Subsystems tab: settings are edited where their status is reported', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue(REPORT);
		listSettingOverrides.mockResolvedValue({ settings: OVERRIDES });
		saveSettingOverride.mockResolvedValue({ status: 'ok' });
		clearSettingOverride.mockResolvedValue({ status: 'ok' });
	});

	it('puts each editable setting on its subsystem card', async () => {
		render(SettingsPage);
		await waitFor(() => expect(listSettingOverrides).toHaveBeenCalled());

		const backupCard = (await waitFor(() => field('artifacts_remote'))).closest('.card');
		expect(backupCard).toBeTruthy();
		expect(backupCard!.textContent).toContain('Artifact backup');
		// The interval belongs to the same card as the push it schedules.
		expect(backupCard!.contains(field('artifacts_push_interval_seconds'))).toBe(true);

		const embedCard = field('embedding_model').closest('.card');
		expect(embedCard!.textContent).toContain('KB embeddings');
	});

	it('gives retention a card of its own - the self-check has no subsystem for it', async () => {
		render(SettingsPage);
		const retention = await waitFor(() => field('retention_days'));
		const card = retention.closest('.card');
		expect(card!.textContent).toContain('Retention');
		expect(card!.contains(field('metrics_retention_days'))).toBe(true);
	});

	it('says where each value comes from and whether a save takes effect now', async () => {
		render(SettingsPage);
		const remote = await waitFor(() => field('artifacts_remote'));
		expect(remote.textContent).toContain('Saved here, in this instance');
		expect(remote.textContent).toContain('takes effect on the next cycle');

		const envLocked = field('embedding_service_url');
		expect(envLocked.textContent).toContain('set by HP_EMBEDDING_SERVICE_URL in the environment');
	});

	it('renders an env-locked setting read-only, with no input to fill in', async () => {
		render(SettingsPage);
		const envLocked = await waitFor(() => field('embedding_service_url'));

		expect(within(envLocked).queryAllByRole('textbox')).toHaveLength(0);
		expect(within(envLocked).queryAllByRole('button')).toHaveLength(0);
		// The value is still SHOWN - an operator has to know what is in force.
		expect(envLocked.textContent).toContain('http://embed.internal:8080/embed');
	});

	it('saves an edited value through the API and re-reads the report', async () => {
		render(SettingsPage);
		await waitFor(() => field('artifacts_push_interval_seconds'));
		getSelfcheck.mockClear();

		const input = within(field('artifacts_push_interval_seconds')).getByRole('spinbutton');
		await userEvent.clear(input);
		await userEvent.type(input, '120');
		await userEvent.click(within(field('artifacts_push_interval_seconds')).getByRole('button', { name: 'Save' }));

		await waitFor(() =>
			expect(saveSettingOverride).toHaveBeenCalledWith('artifacts_push_interval_seconds', 120),
		);
		// The status the value drives is re-read, so the card cannot show a stale
		// verdict next to a fresh setting.
		await waitFor(() => expect(getSelfcheck).toHaveBeenCalled());
	});

	it('surfaces a 409 verbatim instead of a generic failure', async () => {
		saveSettingOverride.mockRejectedValue(
			new ApiError(
				409,
				JSON.stringify({
					detail:
						'artifacts_remote is overridden by HP_ARTIFACTS_REMOTE; records nothing. Unset HP_ARTIFACTS_REMOTE and restart to manage this setting from here.',
				}),
			),
		);
		render(SettingsPage);
		await waitFor(() => field('artifacts_remote'));

		await userEvent.click(within(field('artifacts_remote')).getByRole('button', { name: 'Save' }));

		await waitFor(() =>
			expect(field('artifacts_remote').textContent).toContain(
				'is overridden by HP_ARTIFACTS_REMOTE; records nothing',
			),
		);
	});

	it('offers a reset only for a value this instance saved', async () => {
		render(SettingsPage);
		await waitFor(() => field('artifacts_remote'));

		expect(
			within(field('artifacts_remote')).getByRole('button', { name: /Reset to default/ }),
		).toBeTruthy();
		// A code default has nothing to reset TO.
		expect(
			within(field('embedding_model')).queryByRole('button', { name: /Reset to default/ }),
		).toBeNull();

		await userEvent.click(
			within(field('artifacts_remote')).getByRole('button', { name: /Reset to default/ }),
		);
		await waitFor(() => expect(clearSettingOverride).toHaveBeenCalledWith('artifacts_remote'));
	});

	it('shows a non-admin the values without controls', async () => {
		const { sessionStore } = await import('$lib/api');
		sessionStore.set({
			authenticated: true,
			token_label: 'ro',
			scope: 'read',
			role: 'viewer',
			capabilities: ['read'],
		});
		try {
			render(SettingsPage);
			// The whole tab is admin-only, so there is nothing to edit and nothing
			// pretending to be editable.
			await waitFor(() => expect(screen.getByText(/admin-only/)).toBeTruthy());
			expect(document.querySelectorAll('[data-setting]')).toHaveLength(0);
		} finally {
			sessionStore.set({
				authenticated: true,
				token_label: 'test',
				scope: '*',
				role: 'admin',
				capabilities: ['read', 'write', 'admin'],
			});
		}
	});
});
