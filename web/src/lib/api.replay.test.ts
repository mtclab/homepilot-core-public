import { describe, it, expect, vi, afterEach } from 'vitest';
import { api } from './api';

/**
 * Replay has its own endpoint (#695).
 *
 * The change page's Replay button called `applyArtifact` - `POST
 * /artifacts/{id}/apply` - and the backend refuses to apply anything that is not
 * APPROVED ("Invalid transition: applied -> apply"). Replay is only ever offered
 * on an APPLIED artifact, so the button failed with a 409 every single time.
 * `POST /artifacts/{id}/replay` is the route that runs a replay through the
 * engine's replay guards (`replay_safe: false`, replay-only), and nothing in the
 * client called it.
 *
 * Teeth: point replayArtifact at `/apply` (or delete it) and this fails.
 */
afterEach(() => {
	vi.unstubAllGlobals();
});

describe('api.replayArtifact', () => {
	it('POSTs to /artifacts/{id}/replay, not to /apply', async () => {
		const fetchMock = vi.fn().mockResolvedValue(
			new Response(
				JSON.stringify({ task_id: 't-1', artifact_id: 'a-1', status: 'pending', action: 'replay' }),
				{ status: 202, headers: { 'Content-Type': 'application/json' } },
			),
		);
		vi.stubGlobal('fetch', fetchMock);

		const res = await api.replayArtifact('a-1');

		expect(fetchMock).toHaveBeenCalledTimes(1);
		const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
		expect(url).toMatch(/\/artifacts\/a-1\/replay$/);
		expect(url).not.toMatch(/\/apply/);
		expect((init.method ?? 'GET').toUpperCase()).toBe('POST');
		expect(res.task_id).toBe('t-1');
	});
});
