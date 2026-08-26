/* Datum-Sync web UI.
 *
 * One file, no build step, no framework. Talks only to /rest/v1/ and to the
 * two sign-in routes under /ui/, and carries no credential of its own: the
 * session cookie is HttpOnly, so this code cannot read it and neither can
 * anything injected alongside it.
 *
 * Screens mirror FME Flow's structure (spec/components.md section 11). The
 * ones with no backend yet say so rather than showing an empty table, which
 * would be indistinguishable from a backend that is broken.
 */
'use strict';

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

/* Everything on screen is built with el(). `innerHTML` is not used anywhere
 * in this file, and tests/test_ui.py fails the build if it appears.
 *
 * This is not fastidiousness. Repository names, workspace descriptions, job
 * errors and account names are all data, and a runner exists to execute code
 * other people published -- "it is only our own data" is false here by
 * design. Interpolating any of it into innerHTML is stored XSS. The HttpOnly
 * cookie means such a bug could not steal the credential, but it would not
 * need to: the script runs on the page and can drive the whole API as the
 * signed-in user, including the admin routes.
 */
function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : String(value));
    }
    append(node, children);
    return node;
}

function append(node, children) {
    for (const child of children.flat(Infinity)) {
        if (child === null || child === undefined || child === false) continue;
        // Text, always. A string is never parsed as markup.
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
}

function clear(node) {
    node.replaceChildren();
    return node;
}

const SVG = 'http://www.w3.org/2000/svg';

/* Nav glyphs. Drawn here from primitives rather than loaded from an icon set:
 * the spec asks for Phosphor, and a hand-drawn approximation labelled
 * "Phosphor" would be neither. These are stand-ins until the real woff2/SVG
 * assets are vendored -- deliberately plain, so nobody mistakes them for the
 * finished article. */
const GLYPHS = {
    dashboard:       'M4 4h7v7H4zM13 4h7v5h-7zM13 13h7v7h-7zM4 15h7v5H4z',
    repositories:    'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    jobs:            'M4 6h16M4 12h16M4 18h10',
    schedules:       'M5 5h14v14H5zM5 9h14M9 3v4M15 3v4',
    automations:     'M13 3 5 14h6l-2 7 8-11h-6z',
    connections:     'M9 7V4M15 7V4M7 7h10v5a5 5 0 0 1-10 0zM12 17v4',
    resources:       'M6 3h7l5 5v13H6zM13 3v5h5',
    services:        'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c5 6 5 12 0 18-5-6-5-12 0-18z',
    admin:           'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
    // stub sections
    notifications:   'M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0',
    streams:         'M2 12c1.5-3 3.5-4.5 5-4.5s3.5 1.5 5 4.5 3.5 4.5 5 4.5M2 6c1.5-3 3.5-4.5 5-4.5s3.5 1.5 5 4.5 3.5 4.5 5 4.5',
    'data-virt':     'M4 7c0 1.7 3.6 3 8 3s8-1.3 8-3-3.6-3-8-3-8 1.3-8 3zM4 7v5c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 16v1c0 1.7 3.6 3 8 3s8-1.3 8-3v-1',
    mcp:             'M5 3h14v8H5zM3 11h18M8 11v6M16 11v6M12 11v10',
    'flow-apps':     'M3 3h8v8H3zM13 3h8v8h-8zM3 13h8v8H3zM13 13h8v8h-8z',
    workspaces:      'M2 7l10-4 10 4v10l-10 4-10-4zM2 7l10 4 10-4M12 11v10',
    projects:        'M3 5a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 10v5M9.5 12.5h5',
    analytics:       'M18 20V10M12 20V4M6 20v-6',
    'auth-services': 'M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4',
    'system-config': 'M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41M12 2v2M12 20v2',
    'queue-control': 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
    migration:       'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12',
};

function icon(name) {
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('width', '17');
    svg.setAttribute('height', '17');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', GLYPHS[name] || '');
    svg.append(path);
    return svg;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

class ApiError extends Error {
    constructor(status, code, message) {
        super(message);
        this.status = status;
        this.code = code;
    }
}

async function request(path, options) {
    const opts = Object.assign({ credentials: 'same-origin', headers: {} }, options);
    if (opts.json !== undefined) {
        opts.headers['content-type'] = 'application/json';
        opts.body = JSON.stringify(opts.json);
        delete opts.json;
    }
    const res = await fetch(path, opts);
    if (res.status === 204) return null;
    let body = {};
    try { body = await res.json(); } catch (e) { /* empty or not JSON */ }
    if (!res.ok) {
        throw new ApiError(res.status, body.code || 'ERROR', body.message || res.statusText);
    }
    return body;
}

const api = (path, options) => request('/rest/v1' + path, options);

// ---------------------------------------------------------------------------
// session
// ---------------------------------------------------------------------------

let me = null;

const $ = (id) => document.getElementById(id);

async function start() {
    try {
        me = await api('/whoami');
    } catch (err) {
        // 401 is the ordinary "not signed in" answer. Anything else -- the
        // server down, the database unreachable -- lands on the same screen,
        // because there is nowhere else to put it before sign-in, but it says
        // what happened instead of silently inviting a password that cannot
        // possibly work.
        if (err instanceof ApiError && err.status === 401) return showSignin();
        return showSignin(err.message || String(err));
    }
    showApp();
}

function showSignin(message) {
    me = null;
    $('app').hidden = true;
    $('signin').hidden = false;
    const error = $('signin-error');
    error.hidden = !message;
    error.textContent = message || '';
    $('signin-name').focus();
}

function showApp() {
    $('signin').hidden = true;
    $('app').hidden = false;
    clear($('account'));
    append($('account'), [
        me.name,
        // The server says who it thinks is calling, and with DATUM_SYNC_AUTH=off
        // the honest answer is nobody. Rendered here rather than left in the log
        // because the whole failure mode of that flag is forgetting it is on --
        // and a log you have to go and read is not a reminder.
        me.source === 'auth-disabled'
            ? el('span', { class: 'badge-insecure', title:
                  'DATUM_SYNC_AUTH=off - every request is served as an ' +
                  'administrator, with no credential.' }, 'no auth')
            : me.is_admin ? el('span', { class: 'badge-admin' }, 'admin') : null,
    ]);
    buildNav();
    refreshEngines();
    route();
}

$('signin-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('signin-submit');
    button.disabled = true;
    try {
        me = await request('/ui/login', {
            method: 'POST',
            json: { name: $('signin-name').value, password: $('signin-password').value },
        });
        $('signin-password').value = '';
        $('signin-error').hidden = true;
        showApp();
    } catch (err) {
        showSignin(err.message || 'sign-in failed');
    } finally {
        button.disabled = false;
    }
});

$('signout').addEventListener('click', async () => {
    // Server first: clearing the cookie without revoking leaves a credential
    // that still works for anyone who captured it.
    try { await request('/ui/logout', { method: 'POST' }); } catch (e) { /* sign out anyway */ }
    showSignin();
});

$('nav-toggle').addEventListener('click', () => $('app').classList.toggle('collapsed'));

async function refreshEngines() {
    try {
        const e = await api('/engines');
        clear($('engines'));
        append($('engines'), [
            el('b', {}, e.workers), ' worker', e.workers === 1 ? '' : 's',
            ' \u00b7 ', el('b', {}, e.running), ' running',
            ' \u00b7 ', el('b', {}, e.queued), ' queued',
        ]);
    } catch (err) { /* the header is not worth an error screen */ }
}

// ---------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------

/* Ordered to match FME Flow's rail, so someone who knows Flow finds each
 * section where they expect it. Flow's order, with our equivalents:
 *
 *   Run Workspace / Workspaces   -> Repositories   (its entry point, and ours)
 *   Automations                  -> Automations
 *   Schedules                    -> Schedules
 *   Jobs                         -> Jobs
 *   Connections & Parameters     -> Connections
 *   Resources                    -> Resources
 *   -- (Flow's wider gap before its administration group) --
 *   User Management              -> Admin
 *
 * Services has no Flow counterpart -- nothing in Flow hosts a job's output as
 * a site -- so it sits at the end of the working group rather than being
 * forced into a position Flow does not have.
 */
const SECTIONS = [
    { id: 'dashboard',      label: 'Dashboard' },
    { id: 'repositories',   label: 'Repositories' },
    { id: 'automations',    label: 'Automations' },
    { id: 'notifications',  label: 'Notifications',       stub: true },
    { id: 'streams',        label: 'Streams',             stub: true },
    { id: 'data-virt',      label: 'Data Virtualization', stub: true },
    { id: 'mcp',            label: 'MCP Servers',         stub: true },
    { id: 'flow-apps',      label: 'Flow Apps',           stub: true },
    { id: 'schedules',      label: 'Schedules' },
    { id: 'jobs',           label: 'Jobs' },
    { id: 'workspaces',     label: 'Workspaces',          stub: true },
    { id: 'projects',       label: 'Projects',            stub: true },
    { id: 'connections',    label: 'Connections' },
    { id: 'resources',      label: 'Resources',           stub: true },
    { id: 'analytics',      label: 'Analytics',           stub: true },
    { id: 'services',       label: 'Services' },
    { id: 'admin',          label: 'Admin',               adminOnly: true, group: true },
    { id: 'auth-services',  label: 'Authentication Services', adminOnly: true, stub: true },
    { id: 'system-config',  label: 'System Configuration',   adminOnly: true, stub: true },
    { id: 'queue-control',  label: 'Queue Control',           adminOnly: true, stub: true },
    { id: 'migration',      label: 'Backup & Restore',        adminOnly: true, stub: true },
];

function buildNav() {
    const nav = clear($('nav'));
    for (const section of SECTIONS) {
        // Hidden, not disabled -- and hiding is presentation only. Every route
        // behind Admin checks is_admin for itself; this just declutters.
        if (section.adminOnly && !me.is_admin) continue;
        // The label belongs to the group below it, so hiding Admin hides the
        // label too rather than leaving a divider with nothing under it.
        if (section.group) nav.append(el('div', { class: 'nav-group-label' }, 'ADMIN'));
        nav.append(el('a', {
            href: '#/' + section.id,
            id: 'nav-' + section.id,
        }, icon(section.id), el('span', { class: 'nav-label' }, section.label)));
    }
    nav.append(el('div', { class: 'nav-footer' },
        el('b', {}, 'Datum-Sync'), el('br', {}), 'Workspace runner'));
}

/* Where an empty hash lands. Named because it is written down in three places
 * that have to agree: the hash parser, the section route() picks when there is
 * no hash at all, and the `data-ready` value the browser tests wait on. When
 * they disagreed, the third was the one that lied. */
const HOME = 'dashboard';

function hashQuery() {
    return new URLSearchParams((location.hash.split('?')[1]) || '');
}

function parseHash() {
    // The query string lives inside the hash (`#/jobs?status=running`), so it
    // has to come off before splitting on '/' -- otherwise the section name is
    // "jobs?status=running" and matches no screen.
    const raw = (location.hash || '#/' + HOME).split('?')[0].replace(/^#\/?/, '');
    return raw.split('/').filter(Boolean).map(decodeURIComponent);
}

const SCREENS = {
    dashboard:        [screenDashboard],
    repositories:     [screenRepositories, screenRepository, screenWorkspace],
    jobs:             [screenJobs, screenJob],
    schedules:        [screenSchedules, screenSchedule],
    automations:      [screenAutomations, screenAutomation],
    connections:      [screenConnections, screenConnection],
    services:         [screenServices],
    admin:            [screenAdmin],
    // stub sections — visible in the nav, no backend
    notifications:    [(v) => stubScreen(v, 'Notifications')],
    streams:          [(v) => stubScreen(v, 'Streams')],
    'data-virt':      [(v) => stubScreen(v, 'Data Virtualization')],
    mcp:              [(v) => stubScreen(v, 'MCP Servers')],
    'flow-apps':      [(v) => stubScreen(v, 'Flow Apps')],
    workspaces:       [(v) => stubScreen(v, 'Workspaces')],
    projects:         [(v) => stubScreen(v, 'Projects')],
    resources:        [(v) => stubScreen(v, 'Resources')],
    analytics:        [(v) => stubScreen(v, 'Analytics')],
    'auth-services':  [(v) => stubScreen(v, 'Authentication Services')],
    'system-config':  [(v) => stubScreen(v, 'System Configuration')],
    'queue-control':  [(v) => stubScreen(v, 'Queue Control')],
    migration:        [(v) => stubScreen(v, 'Backup & Restore')],
};

let leaveScreen = null;

// Two routes can be in flight at once. `route()` clears the view synchronously
// but appends after an await, so navigating away before a screen's fetch
// resolves leaves that fetch running -- and it then appends into a view the
// next screen has already filled. The browser smoke caught it as a Schedules
// list carrying a Repositories heading, intermittently, on the one navigation
// fast enough to overlap: the first click after signing in.
//
// The fix is that a screen never gets the view itself, only a container of its
// own that the next route detaches. A stale screen's appends land in an orphan
// node instead of on the page, and progressive rendering still works because
// the container is attached from the start.
let generation = 0;

async function route() {
    if (!me) return;
    // Whatever the last screen left running -- an SSE subscription, a poll --
    // stops before the next one starts. Without this, navigating away from a
    // job detail leaves its EventSource open for the life of the tab.
    if (leaveScreen) { leaveScreen(); leaveScreen = null; }
    const mine = ++generation;

    const parts = parseHash();
    const [section, ...rest] = parts.length ? parts : [HOME];
    for (const link of $('nav').children) {
        link.classList.toggle('active', link.id === 'nav-' + section);
    }

    const screens = SCREENS[section];
    const view = el('div');
    clear($('view')).append(view);
    if (!screens) return void view.append(notBuiltNode('Not found', 'No such screen.'));

    const screen = screens[Math.min(rest.length, screens.length - 1)];
    try {
        const leave = (await screen(view, ...rest)) || null;
        // A stale screen's cleanup belongs to nobody, so run it rather than
        // store it over the live screen's -- otherwise the newer screen's
        // subscription is the one that never gets stopped.
        if (mine !== generation) return void (leave && leave());
        leaveScreen = leave;
        // "This screen, and it is the live one, and it has finished."
        //
        // Every flaky wait in the browser smoke has come from having to infer
        // that from the content -- a heading that every screen has, a button
        // label that two screens share. Those are guesses about rendering;
        // this is the fact itself, and it is set only past the generation
        // check, so a stale screen cannot claim it. Waits belong on this.
        view.dataset.ready = parts.length ? parts.join('/') : HOME;
    } catch (err) {
        if (err instanceof ApiError && err.status === 401) return showSignin();
        view.append(banner(err));
    }
    refreshEngines();
}

window.addEventListener('hashchange', route);

function go(hash) { location.hash = hash; }

function crumbs(...trail) {
    const node = el('div', { class: 'crumbs' });
    trail.forEach(([label, href], i) => {
        if (i) node.append(el('span', {}, '/'));
        node.append(href ? el('a', { href }, label) : el('span', {}, label));
    });
    return node;
}

function notBuiltNode(title, detail) {
    return el('div', {}, el('h1', {}, title), el('div', { class: 'empty' }, detail));
}

function notBuilt(view, title, step) {
    // An honest empty state, not an empty table: a table with no rows looks
    // exactly like a backend that is broken.
    const detail = step
        ? `No API for this yet. It arrives with build step ${step}.`
        : 'No API for this yet.';
    view.append(notBuiltNode(title, detail));
}

/* Stub screen for sections that have no Datum-Sync backend yet. Unlike
 * notBuilt() (which was used internally with step numbers), this is the
 * user-facing empty state for sections that exist in FME Flow but are not
 * part of the current Datum-Sync scope. */
function stubScreen(view, label) {
    view.append(
        el('h1', {}, label),
        el('div', { class: 'stub-empty' },
            el('h2', {}, 'Not yet available'),
            el('p', {}, 'This section is not part of the current Datum-Sync build. '
                + 'Working sections: Repositories, Jobs, Schedules, Automations, '
                + 'Connections, and Services.')));
}

// ---------------------------------------------------------------------------
// formatting
// ---------------------------------------------------------------------------

function when(iso) {
    return iso ? new Date(iso).toLocaleString() : '\u2014';
}

function duration(from, to) {
    if (!from) return '\u2014';
    const ms = (to ? new Date(to) : new Date()) - new Date(from);
    if (ms < 0) return '\u2014';
    const s = Math.round(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

function badge(status) {
    return el('span', { class: 'badge ' + status }, status);
}

function banner(err) {
    return el('div', { class: 'banner' },
        el('b', {}, (err.code || 'ERROR') + ': '), err.message || String(err));
}

function table(headings, rows) {
    return el('table', {},
        el('thead', {}, el('tr', {}, headings.map((h) => el('th', {}, h)))),
        el('tbody', {}, rows));
}

/* Flow's list-page header: the page title, then one row carrying a search
 * field on the left and the action buttons packed against the right. Every
 * list page in Flow is laid out this way, so it is one component here rather
 * than a flex container re-typed in each screen.
 *
 * `search` is a placeholder string; omit it for a page with nothing worth
 * filtering. `actions` is an array of button/link nodes, in Flow's own
 * left-to-right order (create first, destructive last).
 *
 * The filter is client-side, over the rows already rendered. That is a real
 * limitation and not a hidden one: there is no search endpoint, so it matches
 * what was fetched. Every list here fetches in full, which makes the two the
 * same thing today -- but a list that ever paginates server-side would need
 * this to become a query, because otherwise it would silently search only the
 * page on screen and report "no matches" for a row that exists.
 */
function listbar(view, title, opts) {
    const o = opts || {};
    view.append(el('h1', {}, title));
    if (o.subtitle) view.append(el('p', { class: 'subtitle' }, o.subtitle));

    const bar = el('div', { class: 'listbar' });

    if (o.search) {
        const input = el('input', {
            type: 'search',
            placeholder: o.search,
            'aria-label': o.search,
            oninput: () => filterRows(view, input.value),
        });
        bar.append(el('div', { class: 'search' }, input));
    }

    if (o.actions && o.actions.length) {
        bar.append(el('div', { class: 'actions' }, o.actions));
    }

    // A bar with neither a search nor an action is just a margin. Screens that
    // drop both on some paths (the connection create form) would otherwise
    // leave a gap under the title with nothing in it.
    if (bar.firstChild) view.append(bar);
    return bar;
}

/* Hidden, not removed: the row goes back when the term is cleared, and
 * nothing else on the page holds a reference to it that would go stale. */
function filterRows(view, term) {
    const needle = term.trim().toLowerCase();
    for (const row of view.querySelectorAll('tbody tr, .cards > .card')) {
        row.hidden = needle !== '' && !row.textContent.toLowerCase().includes(needle);
    }
}

// ---------------------------------------------------------------------------
// dashboard
// ---------------------------------------------------------------------------

/* Flow's landing page, carrying our data.
 *
 * The structure is theirs, from the 2026.2 pixel scan: a row of create tiles
 * across the top, recent items under it, job counters below those, reference
 * links last. That order is the point. A Flow user arriving here should not
 * have to look for anything, and the previous landing screen -- a bare list of
 * two repositories -- gave them nothing to recognise and nothing to do.
 *
 * The counters carry Flow's labels where the two systems name a state
 * differently ('complete' is 'Successful' there). The status in the link is
 * ours, because that is what /transformations/jobs filters on. */
const COUNTERS = [
    // Flow's grouping too: the three settled states on one row, the two live
    // ones on a wider row under them.
    [['failed', 'Failed'], ['complete', 'Successful'], ['cancelled', 'Cancelled']],
    [['queued', 'Queued'], ['running', 'Running']],
];

async function screenDashboard(view) {
    // Scopes the h2 rule to this screen; elsewhere an h2 is a panel title.
    view.className = 'dashboard';
    view.append(el('h1', {}, 'Dashboard'));

    // Create tiles — local rather than module-level only because of NEW, which
    // is declared further down and would still be in its temporal dead zone.
    const tiles = [
        ['Run Workspace', 'repositories', '#/repositories'],
        ['Create Schedule', 'schedules', '#/schedules/' + NEW],
        ['Create Automation', 'automations', '#/automations/' + NEW],
        // Creating a connection is admin-only at the API. Offering it here
        // unconditionally would put a form nobody may submit one click away.
        me.is_admin && ['Create Connection', 'connections', '#/connections?new=1'],
    ].filter(Boolean);
    view.append(el('div', { class: 'tiles' }, tiles.map(([label, glyph, href]) =>
        el('a', { class: 'tile', href }, icon(glyph), el('span', {}, label)))));

    // Two-column grid: main content on the left, right rail on the right.
    const main = el('div', { class: 'dash-main' });
    const rail = el('div', { class: 'dash-rail' });
    view.append(el('div', { class: 'dash-grid' }, main, rail));

    // All three requests overlap; any one failing fails the whole screen.
    const [reposData, recent, summary] = await Promise.all([
        api('/repositories'),
        api('/transformations/jobs?limit=5'),
        api('/transformations/jobs/summary'),
    ]);

    // Published repositories as workspace cards.
    if (reposData.items.length) {
        main.append(el('h2', {}, 'Repositories'));
        main.append(el('div', { class: 'ws-cards' }, reposData.items.map((repo) =>
            el('a', { class: 'ws-card',
                href: '#/repositories/' + encodeURIComponent(repo.name) },
                icon('repositories'),
                el('span', { class: 'ws-name' }, repo.name),
                el('span', { class: 'ws-meta' },
                    repo.workspaces, ' workspace', repo.workspaces === 1 ? '' : 's')))));
    }

    // Recent jobs table.
    main.append(el('h2', {}, 'Recent jobs'));
    main.append(recent.items.length
        ? table(['Status', 'Workspace', 'Submitted', ''],
            recent.items.map((job) => el('tr', {},
                el('td', {}, badge(job.status)),
                el('td', {}, job.repository + '/' + job.workspace),
                el('td', {}, when(job.submitted_at)),
                el('td', {}, el('a', { href: '#/jobs/' + job.id }, 'open')))))
        : el('div', { class: 'empty' }, 'Nothing has run yet.'));

    // Job counters — links into the filtered job list, same as Flow.
    const counts = summary.counts;
    main.append(el('h2', {}, 'Jobs'));
    main.append(el('div', { class: 'counters' }, COUNTERS.map((row) =>
        el('div', { class: 'counter-row' }, row.map(([status, label]) =>
            el('a', { class: 'counter ' + status, href: '#/jobs?status=' + status },
                el('span', { class: 'label' }, label),
                el('span', { class: 'n' }, counts[status])))))));

    // Right rail — reference link.
    rail.append(el('div', { class: 'rail-card' },
        el('h3', {}, 'Reference'),
        el('a', { class: 'link-card', href: '/docs', target: '_blank', rel: 'noopener' },
            el('div', { class: 'lc-text' },
                el('b', {}, 'REST API'),
                el('span', {}, 'Every route this page calls, with its schema')),
            el('span', { class: 'lc-arrow' }, '\u2192'))));

    // Repoll only while something is moving.
    if (counts.queued || counts.running) {
        const timer = setTimeout(route, 4000);
        return () => clearTimeout(timer);
    }
}

// ---------------------------------------------------------------------------
// repositories
// ---------------------------------------------------------------------------

async function screenRepositories(view) {
    const { items } = await api('/repositories');
    if (!items.length) {
        view.append(el('h1', {}, 'Repositories'));
        view.append(el('div', { class: 'empty' },
            'Nothing published. Use the ', el('code', {}, 'repos'),
            ' CLI to add a repository.'));
        return;
    }
    listbar(view, 'Repositories', { search: 'Search repositories by name' });
    view.append(el('div', { class: 'cards' }, items.map((repo) =>
        el('a', { class: 'card', href: '#/repositories/' + encodeURIComponent(repo.name) },
            el('h3', {}, repo.name),
            el('p', {}, repo.workspaces, ' workspace', repo.workspaces === 1 ? '' : 's'),
            el('div', { class: 'meta' }, repo.path)))));
}

async function screenRepository(view, repo) {
    const { items } = await api(`/repositories/${encodeURIComponent(repo)}/workspaces`);
    view.append(
        crumbs(['Repositories', '#/repositories'], [repo]),
        el('h1', {}, repo));
    if (!items.length) {
        view.append(el('div', { class: 'empty' }, 'No published workspaces.'));
        return;
    }
    view.append(el('div', { class: 'cards' }, items.map((ws) =>
        el('a', {
            class: 'card',
            href: `#/repositories/${encodeURIComponent(repo)}/${encodeURIComponent(ws.name)}`,
        },
            el('h3', {}, ws.name),
            el('p', {}, ws.description || 'No description.'),
            el('div', { class: 'meta' }, 'v', ws.version, ' \u00b7 ', when(ws.published_at))))));
}

// ---------------------------------------------------------------------------
// workspace detail: the parameters form
// ---------------------------------------------------------------------------

/* One control per published parameter, and one reader per control. Keeping
 * these together means a type can never be rendered as one thing and read as
 * another -- the bug that produces a job whose parameters are not what the
 * form showed. */
function control(param) {
    const id = 'param-' + param.name;
    if (param.type === 'BOOLEAN') {
        const input = el('input', { type: 'checkbox', id, checked: param.default === true });
        return { input, read: () => input.checked };
    }
    if (param.type === 'LOOKUP_CHOICE') {
        const select = el('select', { id },
            (param.choices || []).map((c) =>
                el('option', { value: c, selected: c === param.default }, c)));
        return { input: select, read: () => select.value };
    }
    if (param.type === 'FILE') {
        const input = el('input', { type: 'file', id });
        return { input, file: true, read: () => input.files[0] || null };
    }
    if (param.type === 'INTEGER' || param.type === 'FLOAT') {
        const input = el('input', {
            type: 'number', id,
            step: param.type === 'INTEGER' ? '1' : 'any',
            value: param.default === null || param.default === undefined ? '' : param.default,
        });
        return {
            input,
            read: () => {
                if (input.value === '') return null;
                return param.type === 'INTEGER' ? parseInt(input.value, 10) : parseFloat(input.value);
            },
        };
    }
    const input = el('input', {
        type: 'text', id,
        value: param.default === null || param.default === undefined ? '' : param.default,
    });
    return { input, read: () => (input.value === '' ? null : input.value) };
}

function field(param, ctl) {
    const label = el('label', { for: 'param-' + param.name },
        param.name, param.required ? el('span', { class: 'req' }, ' *') : null);
    const hint = el('div', { class: 'hint' },
        param.description || param.type.toLowerCase().replace('_', ' '));
    return param.type === 'BOOLEAN'
        ? el('div', { class: 'field' }, el('div', { class: 'check' }, ctl.input, label), hint)
        : el('div', { class: 'field' }, label, ctl.input, hint);
}

async function screenWorkspace(view, repo, name) {
    const ws = await api(
        `/repositories/${encodeURIComponent(repo)}/workspaces/${encodeURIComponent(name)}`);

    const controls = ws.parameters.map((p) => ({ param: p, ctl: control(p) }));
    const status = el('div', {});
    const run = el('button', { type: 'submit' }, 'Run');

    const form = el('form', { class: 'panel' },
        el('h2', {}, 'Parameters'),
        controls.length
            ? controls.map(({ param, ctl }) => field(param, ctl))
            : el('p', { class: 'subtitle' }, 'This workspace publishes no parameters.'),
        run, status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        run.disabled = true;
        clear(status);
        try {
            const params = await readParams(controls);
            const job = await api(
                `/transformations/submit/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`,
                { method: 'POST', json: { params } });
            go('#/jobs/' + job.id);
        } catch (err) {
            status.append(banner(err));
        } finally {
            run.disabled = false;
        }
    });

    // Filtered by the server, not here: asking for the newest 10 jobs and
    // keeping this workspace's would show nothing whenever ten other jobs ran
    // more recently.
    const { items: recent } = await api('/transformations/jobs?limit=10'
        + `&repository=${encodeURIComponent(repo)}&workspace=${encodeURIComponent(name)}`);

    view.append(
        crumbs(['Repositories', '#/repositories'],
               [repo, '#/repositories/' + encodeURIComponent(repo)],
               [name]),
        el('h1', {}, name),
        el('p', { class: 'subtitle' }, ws.description || 'No description.'),
        el('div', { class: 'split' },
            form,
            el('div', { class: 'panel' },
                el('h2', {}, 'Details'),
                el('dl', { class: 'kv' },
                    el('dt', {}, 'Version'), el('dd', {}, ws.version),
                    el('dt', {}, 'Timeout'), el('dd', {}, ws.timeout_seconds, 's'),
                    el('dt', {}, 'Services'), el('dd', {}, ws.services.join(', ') || '\u2014'),
                    el('dt', {}, 'Outputs'),
                    el('dd', {}, ws.outputs.map((o) => o.name).join(', ') || '\u2014'),
                    el('dt', {}, 'Connections'),
                    el('dd', {}, ws.connections.map((c) => c.name).join(', ') || '\u2014'))),
        ),
        el('h2', { style: 'margin-top:1.5rem' }, 'Recent jobs'),
        recent.length
            ? table(['Status', 'Submitted', 'By', ''], recent.map((job) => el('tr', {},
                el('td', {}, badge(job.status)),
                el('td', {}, when(job.submitted_at)),
                el('td', {}, job.submitted_by),
                el('td', {}, el('a', { href: '#/jobs/' + job.id }, 'open')))))
            : el('div', { class: 'empty' }, 'This workspace has not been run recently.'));
}

/* Read a set of controls into the params object a submit or a schedule takes.
 *
 * Shared by the run form and both schedule forms so that a parameter cannot be
 * read one way when it is run now and another way when it is run at 3am.
 *
 * The FILE branch keys off `instanceof File` rather than off `ctl.file`,
 * because on the schedule edit form a FILE control can also return the upload
 * id already stored -- see paramControls().
 */
async function readParams(controls) {
    const params = {};
    for (const { param, ctl } of controls) {
        const value = ctl.read();
        if (value === null || value === undefined) {
            if (param.required) throw new ApiError(0, 'INVALID_PARAMETER',
                `${param.name} is required`);
            continue;   // absent, so the manifest default applies
        }
        // A FILE parameter carries an upload id, never a path or the bytes:
        // the file goes up first and the job gets the id back.
        params[param.name] = value instanceof File ? await upload(value) : value;
    }
    return params;
}

async function upload(file) {
    const body = new FormData();
    body.append('file', file);
    const stored = await api('/uploads', { method: 'POST', body });
    return stored.id;
}

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------

const STATUSES = ['queued', 'running', 'complete', 'failed', 'cancelled'];
const TERMINAL = ['complete', 'failed', 'cancelled'];

async function screenJobs(view) {
    const filter = hashQuery().get('status') || '';
    const query = filter ? '?status=' + encodeURIComponent(filter) : '';
    const { items } = await api('/transformations/jobs' + query);

    const select = el('select', {},
        el('option', { value: '', selected: !filter }, 'All statuses'),
        STATUSES.map((s) => el('option', { value: s, selected: s === filter }, s)));
    // Redraws via hashchange. Calling route() here as well would render twice,
    // the first time against the hash the browser has not updated yet.
    select.addEventListener('change', () =>
        go(select.value ? '#/jobs?status=' + select.value : '#/jobs'));

    // The status filter is an action, not a search: it re-queries the server,
    // where the search box only filters what is already on screen.
    listbar(view, 'Jobs', {
        search: 'Search jobs by workspace or user',
        actions: [select],
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty' }, 'No jobs', filter ? ' with this status.' : ' yet.'));
        return;
    }

    view.append(table(
        ['Status', 'Workspace', 'Submitted by', 'Submitted', 'Duration', ''],
        items.map((job) => el('tr', {},
            el('td', {}, badge(job.status)),
            el('td', {}, el('a', { href: '#/jobs/' + job.id }, job.repository + '/' + job.workspace)),
            el('td', {}, job.submitted_by),
            el('td', {}, when(job.submitted_at)),
            el('td', {}, duration(job.started_at, job.completed_at)),
            el('td', {}, el('a', { href: '#/jobs/' + job.id }, 'open'))))));

    // Only while something is moving. A finished queue is not repolled.
    if (items.some((j) => j.status === 'queued' || j.status === 'running')) {
        const timer = setTimeout(route, 4000);
        return () => clearTimeout(timer);
    }
}

async function screenJob(view, id) {
    const job = await api('/transformations/jobs/id/' + encodeURIComponent(id));

    const statusCell = el('dd', {}, badge(job.status));
    // Held, not inlined: a status arriving over SSE moves these too. Rendering
    // them once from the first fetch left a job reading COMPLETE with no finish
    // time, forever, which is how this was found.
    const startedCell = el('dd', {}, when(job.started_at));
    const finishedCell = el('dd', {}, when(job.completed_at));
    const artifacts = el('dd', {});
    const log = el('div', { class: 'log' });

    /* Progress is announced but never stored -- datum_sync/jobs.py says why --
     * so there is nothing to draw this from on load. It appears when the first
     * report arrives and is absent again after a reload mid-run. Hidden until
     * then rather than shown at 0%, which would claim knowledge of a job that
     * may report nothing at all. */
    const progressFill = el('div', { class: 'fill', style: 'width:0%' });
    const progressText = el('span', {});
    const progressPct = el('span', {});
    const progress = el('div', { class: 'progress', hidden: true },
        el('div', { class: 'track' }, progressFill),
        el('div', { class: 'caption' }, progressText, progressPct));

    const actions = el('div', { style: 'display:flex;gap:.5rem;margin-top:1rem' });

    function showArtifacts(list) {
        clear(artifacts);
        if (!list || !list.length) return void artifacts.append('\u2014');
        for (const a of list) {
            artifacts.append(el('div', {}, el('a', {
                href: `/rest/v1/transformations/jobs/id/${encodeURIComponent(job.id)}`
                    + `/artifacts/${encodeURIComponent(a.name)}`,
                download: '',
            }, a.name), ' ', el('span', { class: 'hint' }, a.type)));
        }
    }

    function showActions(status) {
        clear(actions);
        if (TERMINAL.includes(status)) {
            actions.append(el('button', {
                onclick: async () => {
                    const next = await api(
                        `/transformations/jobs/id/${encodeURIComponent(job.id)}/resubmit`,
                        { method: 'POST' });
                    go('#/jobs/' + next.id);
                },
            }, 'Resubmit'));
        } else {
            actions.append(el('button', {
                class: 'danger',
                onclick: async (event) => {
                    event.target.disabled = true;
                    await api('/transformations/jobs/id/' + encodeURIComponent(job.id),
                        { method: 'DELETE' });
                },
            }, 'Cancel'));
        }
    }

    showArtifacts(job.artifacts);
    showActions(job.status);

    const error = el('div', {});
    function showError(message) {
        clear(error);
        if (message) error.append(el('div', { class: 'banner' }, message));
    }
    showError(job.error);

    view.append(
        crumbs(['Jobs', '#/jobs'], [job.id.slice(0, 8)]),
        el('h1', {}, job.repository, '/', job.workspace),
        error,
        el('div', { class: 'split' },
            el('div', { class: 'panel' }, el('h2', {}, 'Log'), log),
            el('div', { class: 'panel' },
                el('h2', {}, 'Job'),
                progress,
                el('dl', { class: 'kv' },
                    el('dt', {}, 'Status'), statusCell,
                    el('dt', {}, 'Id'), el('dd', {}, job.id),
                    el('dt', {}, 'By'), el('dd', {}, job.submitted_by),
                    el('dt', {}, 'Submitted'), el('dd', {}, when(job.submitted_at)),
                    el('dt', {}, 'Started'), startedCell,
                    el('dt', {}, 'Finished'), finishedCell,
                    el('dt', {}, 'Parameters'),
                    el('dd', {}, JSON.stringify(job.params)),
                    el('dt', {}, 'Artifacts'), artifacts),
                actions)));

    /* The log arrives over SSE, not by polling. EventSource cannot set an
     * Authorization header, which is the other reason the session cookie
     * exists -- /rest/v1/ accepts it, so this stream authenticates the same
     * way every other request on the page does. */
    const stream = new EventSource(
        `/rest/v1/transformations/jobs/id/${encodeURIComponent(job.id)}/events`,
        { withCredentials: true });

    stream.addEventListener('log', (event) => {
        const entry = JSON.parse(event.data);
        const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
        log.append(el('div', { class: entry.level },
            el('span', { class: 'lvl' }, entry.level), entry.message));
        // Follow the tail only if the reader is already at it; scrolling back
        // to read something should not be undone by the next line.
        if (atBottom) log.scrollTop = log.scrollHeight;
    });

    stream.addEventListener('progress', (event) => {
        const update = JSON.parse(event.data);
        // `pct` is a fraction, 0.0 to 1.0, whatever its name says -- that is
        // what spec/workspace-contract.md publishes and what every workspace
        // emits, so it is the name that is wrong and the name is the contract.
        // Clamped because the number comes from workspace code we do not own.
        const fraction = Number(update.pct);
        if (!Number.isFinite(fraction)) return;
        const percent = Math.max(0, Math.min(100, fraction * 100));
        progress.hidden = false;
        progressFill.style.width = percent + '%';
        clear(progressText).append(update.message || '');
        clear(progressPct).append(Math.round(percent) + '%');
    });

    stream.addEventListener('status', (event) => {
        const update = JSON.parse(event.data);
        // A finished job has no progress to show, and leaving the bar at
        // whatever the last report happened to be says 87% next to COMPLETE.
        if (TERMINAL.includes(update.status)) progress.hidden = true;
        clear(statusCell).append(badge(update.status));
        clear(startedCell).append(when(update.started_at));
        clear(finishedCell).append(when(update.completed_at));
        showActions(update.status);
        showError(update.error);
        if (update.artifacts) showArtifacts(update.artifacts);
        refreshEngines();
    });

    // The server closes the stream when the job reaches a terminal status;
    // the browser would then reconnect on a loop. Nothing further is coming.
    stream.addEventListener('error', () => stream.close());

    return () => stream.close();
}

// ---------------------------------------------------------------------------
// schedules
// ---------------------------------------------------------------------------

/* `#/schedules/new` and `#/automations/new` reach the detail screen with the
 * id "new". Safe because both ids are database integers, so no real row can
 * ever be reached by that URL, and it keeps creating and editing on one screen
 * built from one set of fields -- the alternative is two forms that drift. */
const NEW = 'new';

function every(seconds) {
    for (const [unit, size] of [['d', 86400], ['h', 3600], ['m', 60]]) {
        if (seconds % size === 0) return (seconds / size) + unit;
    }
    return seconds + 's';
}

function triggerOf(schedule) {
    return schedule.cron
        ? el('span', {}, el('code', {}, schedule.cron), ' ',
             el('span', { class: 'hint' }, schedule.timezone))
        : el('span', {}, 'every ', every(schedule.interval_s));
}

function zones(selected) {
    // Intl ships the list, so there is no bundled table to go stale and no
    // third-party fetch. The current value is prepended if the browser does
    // not know it, so editing a schedule can never silently retime it.
    let all;
    try { all = Intl.supportedValuesOf('timeZone'); } catch (e) { all = ['UTC']; }
    return all.includes(selected) ? all : [selected, ...all];
}

/* The trigger half of a schedule form: cron or interval, and the zone the
 * cron is read in. Returned as {node, read} for the same reason control()
 * is -- so a trigger cannot be rendered as one kind and read as another. */
function triggerFields(schedule) {
    const isCron = !schedule || schedule.cron !== null;
    const zone = (schedule && schedule.timezone) || 'Pacific/Auckland';

    const kind = el('select', {},
        el('option', { value: 'cron', selected: isCron }, 'Cron expression'),
        el('option', { value: 'interval', selected: !isCron }, 'Fixed interval'));
    const cron = el('input', {
        type: 'text', placeholder: '0 7 * * 1-5',
        value: (schedule && schedule.cron) || '',
    });
    const seconds = el('input', {
        type: 'number', min: '1',
        value: (schedule && schedule.interval_s) || 900,
    });
    const zoneInput = el('select', {},
        zones(zone).map((z) => el('option', { value: z, selected: z === zone }, z)));

    const cronField = el('div', { class: 'field' },
        el('label', {}, 'Cron expression'), cron,
        el('div', { class: 'hint' }, 'Five fields: minute hour day month weekday.'));
    const intervalField = el('div', { class: 'field' },
        el('label', {}, 'Interval (seconds)'), seconds,
        el('div', { class: 'hint' },
            'A duration, so it does not shift when the clocks do.'));
    const zoneField = el('div', { class: 'field' },
        el('label', {}, 'Timezone'), zoneInput,
        el('div', { class: 'hint' },
            '07:00 here stays 07:00 across a daylight saving change.'));

    function show() {
        const cronNow = kind.value === 'cron';
        cronField.hidden = !cronNow;
        intervalField.hidden = cronNow;
        // An interval is not evaluated in a zone, so offering one would
        // suggest it changes something. It is still sent and still stored.
        zoneField.hidden = !cronNow;
    }
    kind.addEventListener('change', show);
    show();

    return {
        node: el('div', {},
            el('div', { class: 'field' }, el('label', {}, 'Trigger'), kind),
            cronField, intervalField, zoneField),
        read: () => (kind.value === 'cron'
            ? { cron: cron.value.trim(), interval_s: null, timezone: zoneInput.value }
            : { cron: null, interval_s: parseInt(seconds.value, 10),
                timezone: zoneInput.value }),
    };
}

/* Controls for a workspace's parameters, seeded with what a schedule already
 * stores. */
function paramControls(parameters, stored) {
    return parameters.map((p) => {
        const seeded = Object.assign({}, p);
        if (stored && p.name in stored) seeded.default = stored[p.name];
        const ctl = control(seeded);
        if (ctl.file && stored && stored[p.name]) {
            // A file input cannot be given a value, so an untouched FILE field
            // reads as null. On an edit form that would quietly drop the
            // upload the schedule has been running with for weeks, and the
            // next run would fail on a missing required parameter. Hold the
            // stored id and return it unless a new file is actually chosen.
            const held = stored[p.name];
            const chosen = ctl.read;
            ctl.read = () => chosen() || held;
        }
        return { param: seeded, ctl };
    });
}

function paramsPanel(controls) {
    return el('div', {},
        el('h2', { style: 'margin-top:1rem' }, 'Parameters'),
        controls.length
            ? controls.map(({ param, ctl }) => field(param, ctl))
            : el('p', { class: 'subtitle' }, 'This workspace publishes no parameters.'));
}

async function screenSchedules(view) {
    const { items } = await api('/schedules');
    listbar(view, 'Schedules', {
        search: 'Search schedules by name or workspace',
        actions: [el('a', { id: 'create', class: 'button', href: '#/schedules/' + NEW }, 'Create')],
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty' },
            'Nothing scheduled. A schedule runs one workspace on a cron ',
            'expression or a fixed interval.'));
        return;
    }

    view.append(table(
        ['', 'Name', 'Workspace', 'Trigger', 'Next run', 'Last run', ''],
        items.map((s) => el('tr', {},
            el('td', {}, badge(s.enabled ? 'enabled' : 'paused')),
            el('td', {}, el('a', { href: '#/schedules/' + s.id }, s.name)),
            el('td', {}, el('a', {
                href: `#/repositories/${encodeURIComponent(s.repository)}`
                    + `/${encodeURIComponent(s.workspace)}`,
            }, s.repository + '/' + s.workspace)),
            el('td', {}, triggerOf(s)),
            // Only for an enabled schedule. Disabling does not clear next_run,
            // so a paused one still carries whatever time it was paused at --
            // shown, that reads as permanently overdue, which it is not.
            el('td', {}, s.enabled ? when(s.next_run) : '\u2014'),
            el('td', {}, s.last_job
                ? el('a', { href: '#/jobs/' + s.last_job }, when(s.last_run))
                : when(s.last_run)),
            el('td', {}, el('a', { href: '#/schedules/' + s.id }, 'open'))))));
}

async function screenSchedule(view, id) {
    return id === NEW ? newSchedule(view) : editSchedule(view, id);
}

async function newSchedule(view) {
    const { items: repos } = await api('/repositories');

    const name = el('input', { type: 'text', required: true, placeholder: 'nightly-export' });
    const repo = el('select', { required: true },
        el('option', { value: '' }, 'Choose a repository\u2026'),
        repos.map((r) => el('option', { value: r.name }, r.name)));
    const workspace = el('select', { required: true, disabled: true },
        el('option', { value: '' }, '\u2014'));
    const params = el('div', {});
    const trigger = triggerFields(null);
    const status = el('div', {});
    const create = el('button', { type: 'submit', disabled: true }, 'Create schedule');
    let controls = [];

    repo.addEventListener('change', async () => {
        controls = [];
        clear(params);
        create.disabled = true;
        workspace.disabled = !repo.value;
        clear(workspace).append(el('option', { value: '' }, 'Choose a workspace\u2026'));
        if (!repo.value) return;
        const { items } = await api(
            `/repositories/${encodeURIComponent(repo.value)}/workspaces`);
        append(workspace, [items.map((w) => el('option', { value: w.name }, w.name))]);
    });

    workspace.addEventListener('change', async () => {
        controls = [];
        clear(params);
        create.disabled = !workspace.value;
        if (!workspace.value) return;
        const ws = await api(`/repositories/${encodeURIComponent(repo.value)}`
            + `/workspaces/${encodeURIComponent(workspace.value)}`);
        controls = paramControls(ws.parameters, null);
        params.append(paramsPanel(controls));
    });

    const form = el('form', { class: 'panel' },
        el('div', { class: 'field' }, el('label', {}, 'Name'), name,
            el('div', { class: 'hint' }, 'Unique, and how the schedule signs the jobs it submits.')),
        el('div', { class: 'field' }, el('label', {}, 'Repository'), repo),
        el('div', { class: 'field' }, el('label', {}, 'Workspace'), workspace),
        trigger.node,
        params,
        el('div', { style: 'margin-top:1rem' }, create),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        create.disabled = true;
        clear(status);
        try {
            const body = Object.assign({
                name: name.value.trim(),
                repository: repo.value,
                workspace: workspace.value,
                // A FILE parameter on a schedule stores an upload id that has
                // to outlive every run, which is a different lifetime from the
                // one an upload for a single job needs.
                params: await readParams(controls),
                enabled: true,
            }, trigger.read());
            await api('/schedules', { method: 'POST', json: body });
            go('#/schedules');
        } catch (err) {
            status.append(banner(err));
        } finally {
            create.disabled = false;
        }
    });

    view.append(
        crumbs(['Schedules', '#/schedules'], ['New']),
        el('h1', {}, 'New schedule'),
        el('p', { class: 'subtitle' },
            'The workspace and its parameters are checked now, so a schedule ',
            'that could never run is refused here rather than at 3am.'),
        form);
}

async function editSchedule(view, id) {
    const s = await api('/schedules/' + encodeURIComponent(id));

    // A schedule can outlive the workspace it points at -- unpublishing does
    // not delete schedules -- and that is exactly when someone comes to look
    // at it. So a 404 here degrades to a read-only view of the stored params
    // rather than taking the whole screen down with it.
    let parameters = null;
    try {
        const ws = await api(`/repositories/${encodeURIComponent(s.repository)}`
            + `/workspaces/${encodeURIComponent(s.workspace)}`);
        parameters = ws.parameters;
    } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 404) throw err;
    }

    const controls = parameters ? paramControls(parameters, s.params) : [];
    const trigger = triggerFields(s);
    const status = el('div', {});
    const save = el('button', { type: 'submit' }, 'Save');

    const form = el('form', { class: 'panel' },
        el('h2', {}, 'Trigger'),
        trigger.node,
        parameters
            ? paramsPanel(controls)
            : el('div', {},
                el('h2', { style: 'margin-top:1rem' }, 'Parameters'),
                el('div', { class: 'banner' },
                    s.repository, '/', s.workspace, ' is no longer published. ',
                    'The stored parameters are shown but cannot be edited here.'),
                el('pre', { class: 'mono' }, JSON.stringify(s.params, null, 2))),
        el('div', { style: 'margin-top:1rem' }, save),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        clear(status);
        try {
            const body = trigger.read();
            // Omitted, not sent empty, when the manifest could not be loaded:
            // an empty params object would erase what the schedule runs with.
            if (parameters) body.params = await readParams(controls);
            await api('/schedules/' + encodeURIComponent(id),
                { method: 'PATCH', json: body });
            go('#/schedules');
        } catch (err) {
            status.append(banner(err));
        } finally {
            save.disabled = false;
        }
    });

    const toggle = el('button', {
        class: 'secondary',
        onclick: async () => {
            toggle.disabled = true;
            await api('/schedules/' + encodeURIComponent(id),
                { method: 'PATCH', json: { enabled: !s.enabled } });
            route();
        },
    }, s.enabled ? 'Pause' : 'Resume');

    view.append(
        crumbs(['Schedules', '#/schedules'], [s.name]),
        el('div', { class: 'toolbar' },
            el('h1', {}, s.name),
            badge(s.enabled ? 'enabled' : 'paused')),
        el('div', { class: 'split' },
            form,
            el('div', { class: 'panel' },
                el('h2', {}, 'Details'),
                el('dl', { class: 'kv' },
                    el('dt', {}, 'Workspace'),
                    el('dd', {}, el('a', {
                        href: `#/repositories/${encodeURIComponent(s.repository)}`
                            + `/${encodeURIComponent(s.workspace)}`,
                    }, s.repository + '/' + s.workspace)),
                    el('dt', {}, 'Next run'),
                    el('dd', {}, s.enabled ? when(s.next_run) : 'paused'),
                    el('dt', {}, 'Last run'), el('dd', {}, when(s.last_run)),
                    el('dt', {}, 'Last job'),
                    el('dd', {}, s.last_job
                        ? el('a', { href: '#/jobs/' + s.last_job }, s.last_job.slice(0, 8))
                        : '\u2014'),
                    el('dt', {}, 'Created by'), el('dd', {}, s.created_by || '\u2014'),
                    el('dt', {}, 'Created'), el('dd', {}, when(s.created_at))),
                el('p', { class: 'hint', style: 'margin-top:1rem' },
                    'Repository and workspace cannot be changed. A schedule ',
                    'that could be repointed is a permission check made once, ',
                    'on a row that no longer says what it said.'),
                el('div', { style: 'display:flex;gap:.5rem;margin-top:1rem' },
                    toggle,
                    el('button', {
                        class: 'danger',
                        onclick: async (event) => {
                            event.target.disabled = true;
                            await api('/schedules/' + encodeURIComponent(id),
                                { method: 'DELETE' });
                            go('#/schedules');
                        },
                    }, 'Delete')))));
}

// ---------------------------------------------------------------------------
// automations
// ---------------------------------------------------------------------------

/* The document a new automation starts from. Deliberately complete and
 * deliberately not runnable as-is: every field is shown with a real value so
 * the shape is learnable from the form, and REPOSITORY/WORKSPACE are obvious
 * placeholders so nobody saves the example by accident and wonders why it
 * never fires. */
const AUTOMATION_TEMPLATE = [
    'name: my-automation',
    'enabled: true',
    '',
    'trigger:',
    '  type: job_complete',
    '  repository: REPOSITORY',
    '  workspace: WORKSPACE',
    '  status: complete        # omit for any terminal status, failures included',
    '',
    'actions:',
    '  - type: http_request',
    '    method: POST',
    '    url: https://example.com/hook',
    '    body: \'{"job": "{{job.id}}", "status": "{{job.status}}"}\'',
    '',
].join('\n');

function triggerSummary(config) {
    const t = config.trigger;
    return el('span', {},
        (t.repository || 'any') + '/' + (t.workspace || 'any'),
        ' \u00b7 ',
        el('span', { class: 'hint' }, t.status || 'any terminal status'));
}

async function screenAutomations(view) {
    const { items } = await api('/automations');
    listbar(view, 'Automations', {
        search: 'Search automations by name',
        actions: [el('a', { id: 'create', class: 'button', href: '#/automations/' + NEW }, 'Create')],
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty' },
            'No automations. An automation watches for finished jobs and runs ',
            'a workspace or calls a URL when one matches.'));
        return;
    }

    view.append(table(
        ['', 'Name', 'Trigger', 'Actions', 'Last fired', 'Last error', ''],
        items.map((a) => el('tr', {},
            el('td', {}, badge(a.enabled ? 'enabled' : 'paused')),
            el('td', {}, el('a', { href: '#/automations/' + a.id }, a.name)),
            el('td', {}, triggerSummary(a.config)),
            el('td', {}, a.config.actions.map((x) => x.type).join(', ')),
            el('td', {}, when(a.last_fired)),
            el('td', { class: 'error' }, a.last_error || ''),
            el('td', {}, el('a', { href: '#/automations/' + a.id }, 'open'))))));
}

async function screenAutomation(view, id) {
    const fresh = id === NEW;
    const a = fresh ? null : await api('/automations/' + encodeURIComponent(id));

    /* The stored YAML, verbatim -- not the parsed config re-serialised. An
     * editor that hands back a normalised document silently discards comments,
     * key order and quoting style, so opening an automation and saving it
     * unchanged would rewrite it. */
    const editor = el('textarea', { class: 'yaml', spellcheck: 'false', rows: 22 },
        fresh ? AUTOMATION_TEMPLATE : a.yaml);
    const status = el('div', {});
    const save = el('button', { type: 'submit' }, fresh ? 'Create automation' : 'Save');

    const form = el('form', { class: 'panel' },
        el('h2', {}, 'Definition'),
        editor,
        el('div', { style: 'margin-top:1rem' }, save),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        clear(status);
        try {
            await api(fresh ? '/automations' : '/automations/' + encodeURIComponent(id),
                { method: fresh ? 'POST' : 'PUT', json: { yaml: editor.value } });
            go('#/automations');
        } catch (err) {
            // The server parses the YAML; nothing here does. One parser means
            // the editor cannot accept a document the server would refuse, or
            // refuse one it would accept.
            status.append(banner(err));
        } finally {
            save.disabled = false;
        }
    });

    if (fresh) {
        view.append(
            crumbs(['Automations', '#/automations'], ['New']),
            el('h1', {}, 'New automation'),
            el('p', { class: 'subtitle' },
                'Placeholders are ', el('code', {}, '{{job.id}}'), ', ',
                el('code', {}, '{{job.status}}'), ' and ',
                el('code', {}, '{{params.NAME}}'), '. A name outside that set ',
                'is refused now rather than posted as literal text later.'),
            form);
        return;
    }

    const toggle = el('button', {
        class: 'secondary',
        onclick: async () => {
            toggle.disabled = true;
            await api('/automations/' + encodeURIComponent(id),
                { method: 'PATCH', json: { enabled: !a.enabled } });
            route();
        },
    }, a.enabled ? 'Pause' : 'Resume');

    const { items: runs } = await api(
        '/automations/' + encodeURIComponent(id) + '/runs');

    view.append(
        crumbs(['Automations', '#/automations'], [a.name]),
        el('div', { class: 'toolbar' },
            el('h1', {}, a.name),
            badge(a.enabled ? 'enabled' : 'paused')),
        a.last_error ? el('div', { class: 'banner' }, a.last_error) : null,
        el('div', { class: 'split' },
            form,
            el('div', { class: 'panel' },
                el('h2', {}, 'Details'),
                el('dl', { class: 'kv' },
                    el('dt', {}, 'Trigger'), el('dd', {}, triggerSummary(a.config)),
                    el('dt', {}, 'Actions'),
                    el('dd', {}, a.config.actions.map((x) => x.type).join(', ')),
                    el('dt', {}, 'Last fired'), el('dd', {}, when(a.last_fired)),
                    el('dt', {}, 'Created by'), el('dd', {}, a.created_by || '\u2014'),
                    el('dt', {}, 'Created'), el('dd', {}, when(a.created_at)),
                    el('dt', {}, 'Updated'), el('dd', {}, when(a.updated_at))),
                el('div', { style: 'display:flex;gap:.5rem;margin-top:1rem' },
                    toggle,
                    el('button', {
                        class: 'danger',
                        onclick: async (event) => {
                            event.target.disabled = true;
                            await api('/automations/' + encodeURIComponent(id),
                                { method: 'DELETE' });
                            go('#/automations');
                        },
                    }, 'Delete')))),
        el('h2', { style: 'margin-top:1.5rem' }, 'Runs'),
        runs.length
            ? table(['', 'Fired', 'Triggered by', 'Result'], runs.map((r) => el('tr', {},
                el('td', {}, badge(r.ok ? 'complete' : 'failed')),
                el('td', {}, when(r.fired_at)),
                el('td', {}, r.trigger_job
                    ? el('a', { href: '#/jobs/' + r.trigger_job }, r.trigger_job.slice(0, 8))
                    : '\u2014'),
                el('td', {}, el('span', { class: 'mono' }, JSON.stringify(r.results))))))
            : el('div', { class: 'empty' }, 'This automation has not fired yet.'));
}

// ---------------------------------------------------------------------------
// connections
// ---------------------------------------------------------------------------

const CONNECTION_TYPES = ['database', 'http', 'email_smtp', 'email_imap', 'file',
                          'oauth_client'];

/* Shown as a hint, never enforced here. The server validates, and a second copy
 * of the rule in the browser is a second copy that can disagree with it -- the
 * failure mode being a form that refuses something the API would accept, which
 * nobody can debug from the screen. */
const CONFIG_HINTS = {
    database: 'host, port, database, username',
    http: 'base_url',
    email_smtp: 'host, port, username',
    email_imap: 'host, port, username',
    file: 'root',
    oauth_client: 'token_url, client_id',
};

function scopeSummary(c) {
    return c.scope === 'global'
        ? el('span', {}, 'global')
        : el('span', {}, c.scope, ': ',
             el('span', { class: 'mono' }, c.scope_targets.join(', ')));
}

function lastTest(c) {
    if (!c.last_test_at) return el('span', { class: 'hint' }, 'never tested');
    return el('span', {},
        badge(c.last_test_ok ? 'complete' : 'failed'), ' ', when(c.last_test_at));
}

function readJson(box, label) {
    const text = box.value.trim();
    if (!text) return null;
    try {
        return JSON.parse(text);
    } catch (err) {
        throw new ApiError(0, 'INVALID_JSON', `${label} is not valid JSON: ${err.message}`);
    }
}

/* One form for create and edit. They differ in three places -- the name field,
 * the method, and where you land afterwards -- and a second near-identical form
 * is how the two drift apart. */
function connectionForm(c) {
    const fresh = c === null;

    const name = el('input', { type: 'text', required: true, placeholder: 'scimac-postgres' });
    const type = el('select', {}, CONNECTION_TYPES.map((t) =>
        el('option', { value: t, selected: !fresh && c.type === t }, t)));
    const tier = el('select', {}, [1, 2, 3, 4].map((t) =>
        el('option', { value: t, selected: !fresh && c.tier === t }, 'Tier ' + t)));
    const scope = el('select', {}, ['global', 'repository', 'workspace'].map((s) =>
        el('option', { value: s, selected: !fresh && c.scope === s }, s)));
    const targets = el('input', {
        type: 'text',
        value: fresh ? '' : c.scope_targets.join(', '),
        placeholder: 'SCIMAC, Testing',
        disabled: fresh || c.scope === 'global',
    });
    const access = el('select', {}, ['read', 'write'].map((a) =>
        el('option', { value: a, selected: !fresh && c.access === a }, a)));
    const description = el('input', {
        type: 'text', value: (!fresh && c.description) || '',
    });
    const config = el('textarea', { class: 'yaml', spellcheck: 'false', rows: 8 },
        fresh ? '{}' : JSON.stringify(c.config, null, 2));
    const secret = el('textarea', {
        class: 'yaml', spellcheck: 'false', rows: 5,
        placeholder: '{"password": "\u2026"}',
    });
    const configHint = el('div', { class: 'hint' });
    const status = el('div', {});
    const save = el('button', { type: 'submit' }, fresh ? 'Create connection' : 'Save');

    function syncHints() {
        clear(configHint).append(document.createTextNode(
            'Non-secret fields, returned by the API and shown above. Usually: '
            + (CONFIG_HINTS[type.value] || '\u2014')));
        // A global connection carries no targets -- the database refuses the
        // combination -- so the field is disabled rather than ignored.
        targets.disabled = scope.value === 'global';
        if (targets.disabled) targets.value = '';
    }
    type.addEventListener('change', syncHints);
    scope.addEventListener('change', syncHints);
    syncHints();

    const form = el('form', { class: 'panel' },
        el('h2', {}, fresh ? 'New connection' : 'Definition'),
        fresh
            ? el('div', { class: 'field' }, el('label', {}, 'Name'), name,
                 el('div', { class: 'hint' },
                     'Unique, and how a workspace names it in its manifest. It is '
                     + 'also what the secret is sealed against, so it cannot be '
                     + 'changed later.'))
            : null,
        el('div', { class: 'field' }, el('label', {}, 'Type'), type),
        el('div', { class: 'field' }, el('label', {}, 'Tier'), tier,
            el('div', { class: 'hint' },
                'Sensitivity, 1 to 4. Recorded and displayed; not yet enforced '
                + 'against what a service account may reach.')),
        el('div', { class: 'field' }, el('label', {}, 'Scope'), scope),
        el('div', { class: 'field' }, el('label', {}, 'Scope targets'), targets,
            el('div', { class: 'hint' },
                'Comma separated. Repository names, or Repository/Workspace pairs.')),
        el('div', { class: 'field' }, el('label', {}, 'Access'), access),
        el('div', { class: 'field' }, el('label', {}, 'Description'), description),
        el('div', { class: 'field' }, el('label', {}, 'Config'), config, configHint),
        el('div', { class: 'field' }, el('label', {}, 'Secret'), secret,
            el('div', { class: 'hint' },
                'Sealed on save and never returned by any route, so this box '
                + 'starts empty even when a secret is stored. Leave it empty to '
                + 'keep the current one.')),
        el('div', { style: 'margin-top:1rem' }, save),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        clear(status);
        try {
            const body = {
                type: type.value,
                tier: Number(tier.value),
                scope: scope.value,
                scope_targets: targets.value.split(',')
                    .map((s) => s.trim()).filter(Boolean),
                access: access.value,
                description: description.value.trim() || null,
                config: readJson(config, 'Config') || {},
            };
            // Absent means keep. An empty box is therefore not "clear it" --
            // clearing is a separate, deliberate action, because the common
            // case is editing a description on a connection whose password
            // nobody has to hand.
            const sealed = readJson(secret, 'Secret');
            if (sealed !== null) body.secret = sealed;

            if (fresh) {
                body.name = name.value.trim();
                await api('/connections', { method: 'POST', json: body });
                go('#/connections');
            } else {
                await api('/connections/' + encodeURIComponent(c.name),
                    { method: 'PATCH', json: body });
                route();
            }
        } catch (err) {
            status.append(banner(err));
        } finally {
            save.disabled = false;
        }
    });

    return form;
}

async function screenConnections(view) {
    const { items, key_configured } = await api('/connections');
    /* The create form lives on this screen behind `?new=1` rather than at
     * #/connections/new. A connection is addressed by name, not by id as a
     * schedule is, so a `new` path segment would make a connection actually
     * named "new" unreachable from the UI. */
    const creating = hashQuery().has('new');

    listbar(view, 'Connections', {
        search: !creating && items.length ? 'Search connections by name' : null,
        actions: me.is_admin && !creating
            ? [el('a', { id: 'create', class: 'button', href: '#/connections?new=1' }, 'Create')]
            : [],
    });

    if (!key_configured) {
        // Said before the fields are filled in, not as a 500 on save. Without
        // a key nothing can be sealed and nothing already sealed can be opened.
        view.append(el('div', { class: 'banner' },
            el('b', {}, 'No encryption key. '),
            'DATUM_SYNC_SECRET_KEY is not set, so a connection carrying a '
            + 'credential will be refused and stored secrets cannot be opened.'));
    }

    if (creating) {
        view.append(
            connectionForm(null),
            el('p', {}, el('a', { href: '#/connections' }, 'Cancel')));
        return;
    }

    if (!items.length) {
        view.append(el('div', { class: 'empty' },
            'No connections. A connection is a credential the server holds on ',
            'behalf of workspaces, which name it in their manifest and never ',
            'see where it came from.'));
        return;
    }

    view.append(table(
        ['Name', 'Type', 'Tier', 'Scope', 'Access', 'Secret', 'Last test', ''],
        items.map((c) => el('tr', {},
            el('td', {}, el('a', { href: '#/connections/' + encodeURIComponent(c.name) },
                c.name)),
            el('td', {}, c.type),
            el('td', {}, c.tier),
            el('td', {}, scopeSummary(c)),
            el('td', {}, c.access),
            el('td', {}, c.has_secret ? 'stored' : el('span', { class: 'hint' }, 'none')),
            el('td', {}, lastTest(c)),
            el('td', {}, el('a', { href: '#/connections/' + encodeURIComponent(c.name) },
                'open'))))));
}

async function screenConnection(view, name) {
    const c = await api('/connections/' + encodeURIComponent(name));
    const path = '/connections/' + encodeURIComponent(name);
    const failure = el('div', {});

    const test = el('button', {
        class: 'secondary',
        onclick: async () => {
            test.disabled = true;
            clear(failure);
            try {
                await api(path + '/test', { method: 'POST' });
                // The outcome is recorded on the row, so re-reading the screen
                // shows it. Rendering it here as well would give the page two
                // answers that can disagree.
                route();
            } catch (err) {
                failure.append(banner(err));
                test.disabled = false;
            }
        },
    }, 'Test');

    const details = el('div', { class: 'panel' },
        el('h2', {}, 'Details'),
        el('dl', { class: 'kv' },
            el('dt', {}, 'Type'), el('dd', {}, c.type),
            el('dt', {}, 'Tier'), el('dd', {}, c.tier),
            el('dt', {}, 'Scope'), el('dd', {}, scopeSummary(c)),
            el('dt', {}, 'Access'), el('dd', {}, c.access),
            el('dt', {}, 'Secret'), el('dd', {}, c.has_secret ? 'stored' : 'none'),
            el('dt', {}, 'Last test'), el('dd', {}, lastTest(c)),
            el('dt', {}, 'Last error'),
            el('dd', { class: 'error' }, c.last_test_error || '\u2014'),
            el('dt', {}, 'Created by'), el('dd', {}, c.created_by || '\u2014'),
            el('dt', {}, 'Created'), el('dd', {}, when(c.created_at)),
            el('dt', {}, 'Updated'), el('dd', {}, when(c.updated_at))),
        me.is_admin
            ? el('div', { style: 'display:flex;gap:.5rem;margin-top:1rem;flex-wrap:wrap' },
                test,
                c.has_secret
                    ? el('button', {
                        class: 'secondary',
                        onclick: async (event) => {
                            event.target.disabled = true;
                            await api(path, { method: 'PATCH', json: { secret: null } });
                            route();
                        },
                    }, 'Clear secret')
                    : null,
                el('button', {
                    class: 'danger',
                    onclick: async (event) => {
                        event.target.disabled = true;
                        await api(path, { method: 'DELETE' });
                        go('#/connections');
                    },
                }, 'Delete'))
            : null,
        failure);

    view.append(
        crumbs(['Connections', '#/connections'], [c.name]),
        el('div', { class: 'toolbar' },
            el('h1', {}, c.name),
            el('span', { class: 'hint' }, c.type)),
        // Reads are open to any signed-in caller because `config` is what a
        // workspace author needs in order to declare the connection. Writes
        // are admin-only at the API, so a form nobody may submit is not shown.
        me.is_admin
            ? el('div', { class: 'split' }, connectionForm(c), details)
            : details);
}

// ---------------------------------------------------------------------------
// services
// ---------------------------------------------------------------------------

async function screenServices(view) {
    const { items } = await api('/services');

    listbar(view, 'Services', {
        subtitle: 'A hosted service is the one artifact that outlives its job. It is '
            + 'published by a workspace and refreshed by re-running it \u2014 there '
            + 'is nothing to create here.',
        search: items.length ? 'Search services by name or workspace' : null,
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty' },
            'No hosted services. A workspace publishes one by declaring an ',
            'output of type service/static (or /pwa, /dashboard) and returning ',
            'a built directory.'));
        return;
    }

    view.append(table(
        ['Name', 'Type', 'Published by', 'Source job', 'Updated', ''],
        items.map((s) => el('tr', {},
            el('td', {}, s.name),
            el('td', {}, s.type.replace(/^service\//, '')),
            el('td', {}, el('a', {
                href: `#/repositories/${encodeURIComponent(s.repository)}`
                      + `/${encodeURIComponent(s.workspace)}`,
            }, `${s.repository}/${s.workspace}`)),
            el('td', {}, s.source_job
                ? el('a', { href: '#/jobs/' + s.source_job }, s.source_job.slice(0, 8))
                // ON DELETE SET NULL: the job's records can be cleaned up
                // without taking the URL down with them.
                : el('span', { class: 'hint' }, '\u2014')),
            el('td', {}, when(s.updated_at)),
            // A normal link, not an in-app route: the target is a built site
            // served by this server, not a screen of this application.
            el('td', {}, el('a', { href: s.url, target: '_blank' }, 'open'))))));
}

// ---------------------------------------------------------------------------
// admin
// ---------------------------------------------------------------------------

async function screenAdmin(view) {
    const { items } = await api('/accounts');
    view.append(
        el('h1', {}, 'Admin'),
        el('p', { class: 'subtitle' },
            'Accounts are created and tokens minted with the ',
            el('code', {}, 'accounts'), ' CLI. This screen can only revoke.'));

    view.append(table(
        ['Account', 'Tier', 'Scope', 'Credentials', 'Sessions', 'Grants', 'Last used', ''],
        items.map((account) => {
            const revoke = el('button', {
                class: 'danger',
                disabled: !account.sessions && !account.grants,
                onclick: async () => {
                    revoke.disabled = true;
                    await api(`/accounts/${encodeURIComponent(account.name)}/grants`,
                        { method: 'DELETE' });
                    // Revoking your own ends this session too, by design.
                    if (account.name === me.name) return showSignin('Signed out: grants revoked.');
                    route();
                },
            }, 'Revoke');
            return el('tr', {},
                el('td', {},
                    account.name,
                    account.is_admin ? el('span', { class: 'badge-admin' }, ' admin') : null,
                    account.disabled ? el('span', { class: 'error' }, ' disabled') : null),
                el('td', {}, account.max_tier),
                el('td', {}, account.repo_scope ? account.repo_scope.join(', ') : 'all'),
                el('td', {}, [account.has_token ? 'token' : null,
                              account.has_password ? 'password' : null]
                             .filter(Boolean).join(' + ') || '\u2014'),
                el('td', {}, account.sessions),
                el('td', {}, account.grants),
                el('td', {}, when(account.last_used_at)),
                el('td', {}, revoke));
        })));
}

start();
