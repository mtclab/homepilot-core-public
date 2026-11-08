<script context="module" lang="ts">
	export type TagTone = 'muted' | 'ok' | 'warn' | 'crit' | 'accent';
</script>

<script lang="ts">
	// A tag: a short mono word in a box, toned by meaning. The word carries
	// the meaning; the tone repeats it. `muted` is a fact, not a state.
	export let tone: TagTone = 'muted';

	const CLASS: Record<TagTone, string> = {
		muted: '',
		ok: 'tag-ok',
		warn: 'tag-warn',
		crit: 'tag-crit',
		accent: 'tag-accent',
	};
</script>

<span class="tag {CLASS[tone]} {$$props.class ?? ''}"><slot /></span>
