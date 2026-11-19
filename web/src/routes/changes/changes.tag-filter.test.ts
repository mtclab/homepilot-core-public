import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, it, expect, vi, beforeEach } from 'vitest';
// test-mocks MUST come before the component, or the component resolves the real
// $lib/api and never sees the mock.
import '../../lib/test-mocks';
import Changes from './+page.svelte';
import { api } from '$lib/api';

/**
 * The library on the Changes list (#506): a tag filter that asks the SERVER for
 * an exact tag (`?tag=`), with `pattern` - the changes marked worth imitating -
 * always offered, and the other tags as seen on the unfiltered list.
 *
 * Teeth: drop `tag` from the listArtifacts call and the filter test fails.
 */
const listArtifacts = api.listArtifacts as ReturnType<typeof vi.fn>;

const ALL = [
	{ id: 'a-1', kind: 'shell-script', status: 'applied', intent: 'Tune nginx', tags: ['pattern', 'nginx'] },
	{ id: 'a-2', kind: 'shell-script', status: 'applied', intent: 'Other', tags: ['db'] },
	{ id: 'a-3', kind: 'shell-script', status: 'proposed', intent: 'Untagged' },
];

describe('Changes list: the tag filter', () => {
	beforeEach(() => vi.clearAllMocks());

	it('offers pattern and the tags it has seen, and filters on the server', async () => {
		listArtifacts.mockImplementation(async (p: { tag?: string }) => ({
			items: p.tag ? ALL.filter((a) => a.tags?.includes(p.tag as string)) : ALL,
			total: 3,
		}));
		render(Changes);
		const select = (await screen.findByTestId('tag-filter')) as HTMLSelectElement;
		await waitFor(() => expect(select.options.length).toBe(4));
		expect(Array.from(select.options).map((o) => o.value)).toEqual(['', 'pattern', 'db', 'nginx']);

		await fireEvent.change(select, { target: { value: 'pattern' } });
		await waitFor(() => expect(listArtifacts).toHaveBeenLastCalledWith(expect.objectContaining({ tag: 'pattern' })));
		await waitFor(() => expect(screen.queryByText('Untagged')).toBeNull());
		expect(screen.getByText('Tune nginx')).toBeTruthy();
		// The options stay while filtered.
		expect(select.options.length).toBe(4);
	});
});
