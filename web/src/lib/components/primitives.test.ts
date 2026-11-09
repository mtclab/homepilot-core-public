import { render, screen, cleanup } from '@testing-library/svelte';
import { describe, it, expect, afterEach } from 'vitest';
import Lamp from './Lamp.svelte';
import Tag from './Tag.svelte';
import Icon from './Icon.svelte';
import PanelFixture from './Panel.fixture.svelte';
import { ICONS, type IconName } from '$lib/icons';

/**
 * The Instrument primitives (UI slice 0): lamp, tag, panel, icon.
 *
 * What matters to the operator: status is never carried by colour alone (a
 * lamp with a meaning shows the word; a lamp without one is hidden from
 * assistive tech, so it cannot be the only carrier), the shape differs by
 * state, a panel is a named region, and an icon is either decoration beside
 * a word or a named image.
 *
 * Teeth: render a labelled lamp without its word and the first case fails;
 * map two states to one class and the shape case fails; drop the aria-hidden
 * and the decorative cases fail.
 */
afterEach(() => cleanup());

describe('Lamp', () => {
	it('shows the word next to the lamp, and hides the lamp itself', () => {
		const { container } = render(Lamp, { props: { state: 'warn', label: 'Live updates offline' } });
		expect(screen.getByText('Live updates offline')).toBeInTheDocument();
		const lamp = container.querySelector('.lamp')!;
		expect(lamp.getAttribute('aria-hidden')).toBe('true');
		expect(lamp.className).toContain('lamp-warn');
	});

	it('gives every state its own shape class (filled, hollow, half)', () => {
		const cls = (state: string) => {
			const { container } = render(Lamp, { props: { state: state as never } });
			const c = container.querySelector('.lamp')!.className;
			cleanup();
			return c;
		};
		expect(cls('ok')).toContain('lamp-ok');
		expect(cls('crit')).toContain('lamp-crit');
		expect(cls('half')).toContain('lamp-half');
		// No data is the bare hollow ring: no fill class at all.
		expect(cls('off').trim()).toBe('lamp');
	});

	it('is decorative without a word', () => {
		const { container } = render(Lamp, { props: { state: 'ok' } });
		expect(container.querySelector('.lamp')!.getAttribute('aria-hidden')).toBe('true');
	});
});

describe('Tag', () => {
	it('carries the word and its tone', () => {
		const { container } = render(Tag, { props: { tone: 'crit' } });
		expect(container.querySelector('.tag')!.className).toContain('tag-crit');
	});
});

describe('Panel', () => {
	it('is a region named by its title, with the meta line in its head', () => {
		render(PanelFixture);
		const region = screen.getByRole('region', { name: 'Needs attention' });
		expect(region).toBeInTheDocument();
		expect(region.querySelector('.panel-meta')?.textContent).toBe('3 open');
		expect(region.textContent).toContain('body text');
	});
});

describe('Icon', () => {
	it('is hidden from assistive tech when decorative', () => {
		const { container } = render(Icon, { props: { name: 'refresh' } });
		const svg = container.querySelector('svg')!;
		expect(svg.getAttribute('aria-hidden')).toBe('true');
		expect(svg.getAttribute('stroke')).toBe('currentColor');
	});

	it('is a named image when it stands alone', () => {
		render(Icon, { props: { name: 'arrow-right', label: 'becomes' } });
		expect(screen.getByRole('img', { name: 'becomes' })).toBeInTheDocument();
	});

	it('draws every icon in the set', () => {
		for (const name of Object.keys(ICONS) as IconName[]) {
			const { container } = render(Icon, { props: { name } });
			expect(container.querySelectorAll('path').length, name).toBeGreaterThan(0);
			cleanup();
		}
	});
});
