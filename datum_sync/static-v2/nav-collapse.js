/* Nav collapse — the icon rail behind the hamburger.
 *
 * Shared by every mockup so the behaviour has one definition, and so
 * static-v2/app.js can adopt it unchanged rather than growing its own copy.
 *
 * The state is persisted. Flow remembers a collapsed nav across page loads,
 * and a rail that silently springs back open on every navigation is a
 * different, more annoying feature.
 */
(function () {
    'use strict';

    var KEY = 'datum-sync.nav-collapsed';
    var app = document.getElementById('app');
    var btn = document.getElementById('nav-toggle');
    if (!app || !btn) return;

    function apply(collapsed) {
        app.classList.toggle('collapsed', collapsed);
        // The button is the only way back out, so it has to say which way it
        // goes. Its label is wrong in one of the two states otherwise.
        btn.setAttribute('aria-expanded', String(!collapsed));
        btn.setAttribute('aria-label',
            collapsed ? 'Expand navigation' : 'Collapse navigation');
    }

    // Private-mode Safari throws on localStorage rather than returning null,
    // and a dead nav toggle is a worse outcome than a forgotten preference.
    function stored() {
        try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; }
    }
    function remember(collapsed) {
        try { localStorage.setItem(KEY, collapsed ? '1' : '0'); } catch (e) { /* ignore */ }
    }

    apply(stored());

    btn.addEventListener('click', function () {
        var collapsed = !app.classList.contains('collapsed');
        apply(collapsed);
        remember(collapsed);
    });
})();
