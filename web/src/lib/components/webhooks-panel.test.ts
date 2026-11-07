import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '../test-mocks';
import WebhooksPanel from './WebhooksPanel.svelte';
import { api } from '$lib/api';
import { notify } from '$lib/stores';

/**
 * Webhooks are configurable on a running install (#700).
 *
 * They lived only in a CLI that opened the database the backend holds, so on
 * the shipped deploy there was no way to add one. The panel is the console's
 * way in: it lists endpoints by host only, shows a new endpoint's full URL
 * exactly once, offers only event names the platform emits, and a failed test
 * delivery is reported as a failure.
 *
 * Teeth: render `h.url` instead of `h.endpoint` and the redaction case fails;
 * report every test as delivered and the NOT-accepted case fails; drop the
 * unread state and the failed-list case shows "No webhooks yet".
 */
const listWebhooks = api.listWebhooks as ReturnType<typeof vi.fn>;
const createWebhook = api.createWebhook as ReturnType<typeof vi.fn>;
const testWebhook = api.testWebhook as ReturnType<typeof vi.fn>;
const notifyMock = notify as unknown as ReturnType<typeof vi.fn>;

const LISTING = {
	items: [
		{
			id: 3,
			endpoint: 'https://hooks.example.com',
			event_types: ['artifact_proposed'],
			enabled: true,
			max_retries: 3,
			has_secret: true,
			created_at: '2026-09-24T10:00:00Z',
		},
	],
	total: 1,
	event_types: ['artifact_applied', 'artifact_proposed'],
	wildcard: '*',
};

describe('WebhooksPanel', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		listWebhooks.mockResolvedValue(LISTING);
	});

	it('lists an endpoint by its host, never by its path', async () => {
		render(WebhooksPanel);
		expect(await screen.findByText('https://hooks.example.com')).toBeInTheDocument();
		expect(document.body.textContent).not.toContain('/hooks/');
	});

	it('creates with the picked events and shows the full URL once', async () => {
		createWebhook.mockResolvedValue({ ...LISTING.items[0], id: 4, url: 'https://hooks.example.com/hooks/abc' });
		render(WebhooksPanel);
		await screen.findByText('https://hooks.example.com');

		await fireEvent.input(screen.getByPlaceholderText(/hooks\.example\.com/), {
			target: { value: 'https://hooks.example.com/hooks/abc' },
		});
		await fireEvent.click(screen.getByRole('checkbox', { name: /every event/i }));
		await fireEvent.click(await screen.findByRole('checkbox', { name: 'artifact_applied' }));
		await fireEvent.click(screen.getByRole('button', { name: /add webhook/i }));

		await waitFor(() => expect(createWebhook).toHaveBeenCalled());
		expect(createWebhook.mock.calls[0][0]).toMatchObject({
			url: 'https://hooks.example.com/hooks/abc',
			event_types: ['artifact_applied'],
		});
		expect(await screen.findByText('https://hooks.example.com/hooks/abc')).toBeInTheDocument();
	});

	it('says a test delivery that was not accepted was not accepted', async () => {
		testWebhook.mockResolvedValue({ id: 3, endpoint: 'https://hooks.example.com', event: 'webhook_test', delivered: false });
		render(WebhooksPanel);
		await fireEvent.click(await screen.findByRole('button', { name: /send test/i }));

		await waitFor(() => expect(notifyMock).toHaveBeenCalled());
		expect(notifyMock.mock.calls[0][0]).toMatch(/NOT accepted/);
		expect(notifyMock.mock.calls[0][1]).toBe('err');
	});

	it('does not call an unreadable list empty', async () => {
		listWebhooks.mockRejectedValue(new Error('403'));
		render(WebhooksPanel);

		expect(await screen.findByRole('alert')).toHaveTextContent(/could not read the webhooks/i);
		expect(screen.queryByText(/no webhooks yet/i)).not.toBeInTheDocument();
	});
});
