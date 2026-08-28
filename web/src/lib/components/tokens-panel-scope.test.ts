import { render, screen } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import TokensPanel from './TokensPanel.svelte';

/**
 * The token ladder must mint what it advertises (#614, born of #579).
 *
 * The failure this forbids: the console's middle rung said "full - can change
 * things" (write-tier language) but posted scope='full', which the backend
 * normalizes to '*' - the SUPERUSER scope, strictly above the 'admin' option
 * beside it. An operator following the UI's own advice minted a
 * token-managing, secret-reading credential believing it was a write token.
 *
 * So: the middle option's VALUE is 'read,write' (which normalizes to exactly
 * read+write), superuser is not offered here at all, and a legacy '*'/'full'/
 * 'all' token is displayed as 'all' - never as the write rung.
 */

vi.mock('$lib/api', async (importOriginal) => {
	const mod = await importOriginal<typeof import('$lib/api')>();
	return {
		...mod,
		api: {
			...mod.api,
			listTokens: vi.fn().mockResolvedValue({
				items: [
					{ prefix: 'hp_legacyfull0000', label: 'old-superuser', scope: 'full', role: null, created_at: '2026-01-01T00:00:00Z', last_used_at: null, expires_at: null },
					{ prefix: 'hp_writetoken000', label: 'writer', scope: 'read,write', role: null, created_at: '2026-01-01T00:00:00Z', last_used_at: null, expires_at: null },
				],
				total: 2,
			}),
		},
	};
});

describe('the token ladder mints what it advertises', () => {
	beforeEach(() => vi.clearAllMocks());

	it('offers read_only, write (read,write) and admin - and no superuser rung', async () => {
		render(TokensPanel);
		const create = await screen.findByRole('button', { name: /create token/i });
		create.click();
		const select = (await screen.findByLabelText(/scope/i)) as HTMLSelectElement;
		const values = Array.from(select.options).map((o) => o.value);
		expect(values).toContain('read,write');
		expect(values).not.toContain('full');
		expect(values).not.toContain('all');
		expect(values).not.toContain('*');
	});

	it('shows a legacy superuser token as all, never as the write rung', async () => {
		render(TokensPanel);
		expect(await screen.findByText('all')).toBeTruthy();
		const badges = screen.getAllByText(/^(all|write|read_only)$/);
		const texts = badges.map((b) => b.textContent);
		expect(texts).toContain('all');
		expect(texts).toContain('write');
	});
});
