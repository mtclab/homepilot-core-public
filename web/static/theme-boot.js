// Paints the operator's theme before first paint (src/lib/theme.ts is the
// source of truth; src/lib/theme.test.ts asserts this file agrees with it).
// An external file because the CSP is script-src 'self'.
(function () {
	var pref = 'night';
	try {
		var v = window.localStorage.getItem('hp-theme');
		if (v === 'system' || v === 'night' || v === 'day') pref = v;
	} catch (e) {
		/* storage unavailable: the default stands */
	}
	var theme = pref;
	if (pref === 'system') {
		var light = false;
		try {
			light = window.matchMedia('(prefers-color-scheme: light)').matches;
		} catch (e) {
			/* no media queries: Night */
		}
		theme = light ? 'day' : 'night';
	}
	document.documentElement.setAttribute('data-theme', theme);
	document.documentElement.setAttribute('data-theme-pref', pref);
})();
