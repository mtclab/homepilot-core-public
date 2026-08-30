import { render, cleanup } from '@testing-library/svelte';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import { setPage } from '../../lib/test-mocks';
import SettingsPage from './+page.svelte';
import { api, refreshSessionResult } from '$lib/api';
import { notify } from '$lib/stores';

/**
 * Saving a token when the check could not be made (#648 tranche 7).
 *
 * Settings logs in with the pasted token; if the cookie cannot be set it falls
 * back to the in-memory bearer and re-reads the session to confirm the token
 * actually works. That re-read returning nothing used to mean one thing to the
 * UI - "the token was rejected" - when it in fact means either of two: the
 * server ANSWERED that the credential is no good, or nothing answered at all.
 *
 * Told the first when the second is true, the operator goes and rotates a
 * credential that was never the problem, while the real fault (HomePilot is
 * down) goes unmentioned. It is the same belief as #642: a conclusion stated
 * without being established.
 *
 * Teeth: collapse the two failures back into one message and the unreachable
 * case fails on the word "rejected"; drop the rejected case's message and the
 * second test fails - the honest verdict has to stay reachable too.
 */
const login = api.login as ReturnType<typeof vi.fn>;
const sessionResult = refreshSessionResult as unknown as ReturnType<typeof vi.fn>;
const notified = notify as unknown as ReturnType<typeof vi.fn>;

/** Paste a token into Connection and press Save. */
async function saveToken(): Promise<void> {
	const user = userEvent.setup();
	const { container } = render(SettingsPage);
	const field = container.querySelector('#token');
	if (!(field instanceof HTMLInputElement)) throw new Error('no token field');
	await user.type(field, 'hp_tok_example');
	const save = Array.from(container.querySelectorAll('button')).find(
		(b) => b.textContent?.trim() === 'Save',
	);
	if (!save) throw new Error('no Save button');
	await user.click(save);
}

/** Every message the page raised, joined - the toast lives in the layout. */
function messages(): string {
	return notified.mock.calls.map((c) => String(c[0])).join('\n');
}

describe('Settings: a token check that could not be made is not a rejection', () => {
	afterEach(() => cleanup());

	beforeEach(() => {
		vi.clearAllMocks();
		setPage('/ui/settings');
		// The cookie login fails, so the page falls back to the in-memory token
		// and re-reads the session to see whether the token is any good.
		login.mockRejectedValue(new Error('cross-origin: no cookie'));
	});

	it('does not blame the token when nothing answered', async () => {
		sessionResult.mockResolvedValue({ me: null, failure: 'unreachable' });

		await saveToken();

		expect(messages()).toMatch(/could not be reached/i);
		expect(messages()).not.toMatch(/rejected/i);
	});

	it('still says rejected when the server actually rejected it', async () => {
		// The honest verdict must stay reachable, or the fix would only have
		// swapped one wrong sentence for another.
		sessionResult.mockResolvedValue({ me: null, failure: 'rejected' });

		await saveToken();

		expect(messages()).toMatch(/rejected/i);
		expect(messages()).not.toMatch(/could not be reached/i);
	});
});
