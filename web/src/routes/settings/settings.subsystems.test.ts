import { render, screen, waitFor, cleanup, within } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { api } from '$lib/api';

/**
 * THE F6 GATE (#549): every subsystem shows a truthful status sourced from the
 * self-check, and a failing one names its reason.
 *
 * The old Optional-subsystems list said `unreachable` and moved on; an operator
 * reading it learned that something was wrong, not what it cost or where to
 * look. Each card here carries the report's own consequence sentence VERBATIM
 * (the server writes one per state) plus, when the reaching is what failed, the
 * address it could not reach.
 *
 * Teeth: drop the consequence paragraph and every state's card fails; paint the
 * unreachable chip neutral and the "not a grey mystery" case fails; hide the
 * target and the unreachable case fails; paraphrase a consequence and the
 * verbatim assertions fail.
 */
const getSelfcheck = api.getSelfcheck as ReturnType<typeof vi.fn>;

const REPORT = {
	timeout_seconds: 2,
	counts: { ok: 1, off: 1, unreachable: 1, unknown: 1 },
	subsystems: [
		{
			name: 'agent_hub',
			configured: true,
			state: 'ok',
			target: 'wss://hub.example:9443',
			consequence: 'Agents can reach the hub and enrol.',
		},
		{
			name: 'embeddings',
			configured: false,
			state: 'off',
			target: '',
			consequence: 'KB search stays keyword-only. Nothing else is affected.',
		},
		{
			name: 'events_webhook',
			configured: true,
			state: 'unreachable',
			target: 'https://hooks.example/homepilot',
			consequence: 'Events are configured but the endpoint does not answer, so notifications are being lost.',
		},
		{
			name: 'artifacts_remote',
			configured: true,
			state: 'unknown',
			target: 'ssh://backup.example:22',
			consequence: 'Could not check the artifact backup within 2s. Treat it as unproven, not as working.',
		},
	],
};

/** The card whose heading is `name`. */
function card(name: string): HTMLElement {
	const heading = screen.getByRole('heading', { name });
	const el = heading.closest('.card');
	if (!(el instanceof HTMLElement)) throw new Error(`no card around ${name}`);
	return el;
}

describe('Subsystems tab: one truthful status card per subsystem', () => {
	afterEach(() => cleanup());
	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings?tab=subsystems');
		getSelfcheck.mockResolvedValue(REPORT);
	});

	it('renders a card per subsystem with its chip and its consequence, verbatim', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getSelfcheck).toHaveBeenCalled());

		const expected: Array<[string, string, string]> = [
			['Agent hub', 'ok', REPORT.subsystems[0].consequence],
			['KB embeddings', 'off', REPORT.subsystems[1].consequence],
			['Events webhook', 'configured, unreachable', REPORT.subsystems[2].consequence],
			['Artifact backup', 'unverified', REPORT.subsystems[3].consequence],
		];
		for (const [label, chip, consequence] of expected) {
			const c = await waitFor(() => card(label));
			expect(within(c).getByText(chip)).toBeTruthy();
			expect(within(c).getByText(consequence)).toBeTruthy();
		}
	});

	it('names the address an unreachable subsystem could not reach', async () => {
		render(SettingsPage);
		const c = await waitFor(() => card('Events webhook'));
		expect(within(c).getByText('https://hooks.example/homepilot')).toBeTruthy();
		expect(c.textContent).toContain('Could not reach');
	});

	it('gives a failing subsystem an alarming chip and off a calm one', async () => {
		render(SettingsPage);
		await waitFor(() => card('Events webhook'));

		// Not a grey mystery: the failing chip carries the danger family.
		const failing = within(card('Events webhook')).getByText('configured, unreachable');
		expect(failing.className).toContain('badge-critical');

		// off is a CHOICE, so it is neutral - never danger or warning - and has
		// no address to chase.
		const off = within(card('KB embeddings')).getByText('off');
		expect(off.className).toContain('badge-revoked');
		expect(off.className).not.toContain('badge-critical');
		expect(off.className).not.toContain('badge-warning');

		const ok = within(card('Agent hub')).getByText('ok');
		expect(ok.className).toContain('badge-applied');
	});

	it('says when the report was taken and can re-check on demand', async () => {
		render(SettingsPage);
		await waitFor(() => expect(getSelfcheck).toHaveBeenCalledTimes(1));
		await waitFor(() => expect(screen.getByText(/^Checked /)).toBeTruthy());

		await userEvent.click(screen.getByRole('button', { name: /Re-check/ }));
		await waitFor(() => expect(getSelfcheck).toHaveBeenCalledTimes(2));
	});

	it('says the report could not be loaded rather than showing nothing', async () => {
		getSelfcheck.mockRejectedValue(new Error('403 Forbidden'));
		render(SettingsPage);
		await waitFor(() => expect(screen.getByText(/403 Forbidden/)).toBeTruthy());
	});

	it('offers edit controls only for settings the server sends (C2)', async () => {
		// C2 made this tab configure as well as report - but only what the
		// server's registry actually offers. With no editable settings in the
		// report, Re-check is still the only button and there is nothing to type
		// into: the page must never invent a control for a setting the server
		// does not know about. (The controls themselves are gated in
		// settings.overrides.test.ts, which seeds them.)
		//
		// The guest-network card's own re-survey button joined the budget in
		// #553; it READS the cluster and changes nothing, and it is named for
		// what it re-checks so it cannot be confused with the report's Re-check.
		// The point of the assertion is unchanged: no control appears for a
		// setting the server did not send.
		render(SettingsPage);
		await waitFor(() => card('Events webhook'));
		const panel = document.getElementById('settings-panel') as HTMLElement;
		expect(within(panel).getAllByRole('button').map((b) => b.textContent?.trim())).toEqual([
			'↻ Re-check',
			'↻ Re-survey the cluster',
		]);
		expect(within(panel).queryAllByRole('textbox')).toHaveLength(0);
	});
});
