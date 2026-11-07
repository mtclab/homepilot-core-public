import { describe, it, expect } from 'vitest';
import { scopeTier } from './scopeTier';

/**
 * #704: the sidebar badge mapped `*`/`full`/`read_only` - spellings tokens are
 * no longer minted with - so every real `all`/`admin`/`read,write`/`read` token
 * fell through to the muted unknown style. Teeth: put the old string mapping
 * back and the `all`, `admin` and `read,write` cases fail.
 */
describe('scopeTier', () => {
	it('names the superuser scope by any of its spellings', () => {
		for (const s of ['all', '*', 'full']) {
			expect(scopeTier(['read', 'write', 'admin'], s)).toEqual({ label: 'all', cls: 'text-ok' });
		}
	});

	it('reads the tier off the normalized capabilities', () => {
		expect(scopeTier(['read', 'write', 'admin'], 'admin')?.label).toBe('admin');
		expect(scopeTier(['read', 'write'], 'read,write')?.label).toBe('read, write');
		expect(scopeTier(['read', 'write'], 'write,read')?.label).toBe('read, write');
		expect(scopeTier(['read'], 'read')?.label).toBe('read only');
		expect(scopeTier(['read'], 'read_only')?.label).toBe('read only');
	});

	it('says nothing rather than guess when the server sent no capabilities', () => {
		expect(scopeTier(undefined, 'read,write')).toBeNull();
		expect(scopeTier([], null)).toBeNull();
	});
});
