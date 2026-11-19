import { render, screen } from '@testing-library/svelte';
import { describe, it, expect } from 'vitest';
import { encode } from 'uqr';
import QrCode from './QrCode.svelte';

/**
 * The subscribe QR code (#822) is drawn in the browser from `uqr`'s module
 * matrix - no hosted QR service ever sees the link.
 *
 * Asserted against the library's own matrix: every dark module is one unit
 * square in the path and nothing else is, on a white field. Teeth: draw light
 * modules, drop the white background, or offset the squares and it fails.
 */
describe('QrCode', () => {
	it('draws exactly the dark modules of the encoded link', () => {
		const value = 'ntfy://ntfy.example.com/homepilot?display=HomePilot';
		render(QrCode, { props: { value, label: 'Subscribe link' } });
		const svg = screen.getByRole('img', { name: 'Subscribe link' });
		const qr = encode(value, { ecc: 'M', border: 2 });
		expect(svg.getAttribute('viewBox')).toBe(`0 0 ${qr.size} ${qr.size}`);
		const rect = svg.querySelector('rect');
		expect(rect?.getAttribute('fill')).toBe('#fff');
		const d = svg.querySelector('path')?.getAttribute('d') ?? '';
		const drawn = new Set([...d.matchAll(/M(\d+) (\d+)h1v1h-1z/g)].map((m) => `${m[1]},${m[2]}`));
		const dark = new Set<string>();
		qr.data.forEach((row, y) => row.forEach((on, x) => on && dark.add(`${x},${y}`)));
		expect(drawn.size).toBeGreaterThan(100);
		expect(drawn).toEqual(dark);
	});
});
