/* Datum-Sync web UI, v2.
 *
 * The port of static/app.js onto the v2 stylesheet. Same architecture: one
 * file, no build step, no framework, talking only to /rest/v1/ and the two
 * sign-in routes under /ui/, and carrying no credential of its own -- the
 * session cookie is HttpOnly, so this code cannot read it and neither can
 * anything injected alongside it.
 *
 * This is chunk 1 of static-v2/PORT.md: the shell. Screens arrive in chunks
 * 2-10; until then their sections route to portPending() and say so.
 *
 * An ES module, unlike v1, for one reason: the icons. v1 drew its own glyphs
 * from primitives and said in a comment that they were stand-ins. v2 has the
 * real vendored Phosphor set in icons.js, and importing it is the only way to
 * get it here without a second copy that could drift.
 *
 * Three things in v2 that app.js must NOT do, because something else already
 * does them. Each would be invisible rather than broken:
 *
 *   - The nav toggle. nav-collapse.js binds it and persists the state.
 *     Porting v1's line 217 as well means two handlers on one click, and a
 *     toggle that flips twice looks exactly like a toggle that is dead.
 *   - The list search box. table.js binds it, and filters by rebuilding the
 *     tbody. v1's filterRows() hides rows in place instead; running both
 *     leaves table.js re-appending rows v1 hid, so the filter half-works and
 *     the pager's count disagrees with what is on screen.
 *   - Sorting, selection and paging generally. table.js owns the tbody from
 *     mountTable() onwards.
 */
import { ICONS } from '/v2/icons.js';

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

/* Phosphor paths, lifted out of icons.js's markup at load.
 *
 * icons.js stores each icon as the string `<path d="..."/>`, which is the
 * shape the mockup generator wants: it writes files, so a string is the
 * finished article there. Here it is not -- assigning it would mean
 * innerHTML, and the rule above has no exception for "our own constant",
 * because a rule with an exception is a rule someone will extend.
 *
 * Every one of the 51 icons is exactly one <path> carrying only a `d`. So the
 * `d` is all there is to carry, and this turns the markup back into data that
 * createElementNS can take.
 *
 * That "every one" is the load-bearing part, and it is not enforced here: an
 * icon that ever gained a second element would have it silently dropped, and
 * a dropped element renders as a slightly wrong icon, not as an error. It is
 * checked in tests/test_ui.py instead -- over the whole file, at build time,
 * rather than per-call on a value we already decided to trust.
 *
 * icons.js also exports an icon() of its own, and it is deliberately not used:
 * it assigns svg.innerHTML. The string is its own constant so it is not an
 * injection, but the rule above is worth more intact than the 20 lines below
 * are worth saving. A ban with one sanctioned exception is a ban that gets
 * extended.
 */
const ICON_PATHS = Object.fromEntries(
    Object.entries(ICONS).map(([name, markup]) => {
        const match = /<path d="([^"]+)"\/>/.exec(markup);
        return [name, match ? match[1] : null];
    })
);

/* Filled outlines on a 256 viewBox -- NOT v1's stroke geometry. icons.js says
 * so at the top of the file: drawing these with fill:none and stroke-width:2
 * on a 24 viewBox renders a clipped fragment, not a smaller icon. */
function icon(name, size, cls) {
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('width', String(size || 17));
    svg.setAttribute('height', String(size || 17));
    svg.setAttribute('viewBox', '0 0 256 256');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    if (cls) svg.setAttribute('class', cls);
    const d = ICON_PATHS[name];
    if (d) {
        const path = document.createElementNS(SVG, 'path');
        path.setAttribute('d', d);
        svg.append(path);
    }
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
    // Avatar initial from username, shown in the topbar circle button. The
    // mockup draws two letters, but it drew a person's name; /whoami returns
    // one account name, and splitting it into initials would be invention.
    $('avatar-btn').textContent = (me.name || '?')[0].toUpperCase();
    // Account details rendered inside the dropdown panel.
    clear($('account'));
    append($('account'), [
        el('b', {}, me.name),
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

// No handler for #nav-toggle here. nav-collapse.js has it, and adds the
// persistence v1 never had. See the header comment.

// Avatar button toggles the account dropdown; clicking anywhere else closes it.
$('avatar-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    const dd = $('tb-dropdown');
    const open = !dd.hidden;
    dd.hidden = open;
    $('avatar-btn').setAttribute('aria-expanded', String(!open));
});
document.addEventListener('click', () => {
    const dd = $('tb-dropdown');
    if (dd && !dd.hidden) {
        dd.hidden = true;
        $('avatar-btn').setAttribute('aria-expanded', 'false');
    }
});

async function refreshEngines() {
    try {
        const e = await api('/engines');
        clear($('engines'));
        // "engines", following the mockup: it is Flow's word for the thing and
        // the reader's word for it. The API field is `workers`; nothing behind
        // this line changed. Queued is kept -- the mockup dropped it, but a
        // queue depth nobody can see is the number people ask for first.
        append($('engines'), [
            icon('engine', 15), ' ',
            el('b', {}, e.workers), ' engine', e.workers === 1 ? '' : 's',
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
 *
 * Labels carry no vendor's name: Flow's "Flow Apps" is "Apps" here, and its
 * "Data Virtualization" is "API endpoints". Apps is still a stub, and it
 * overlaps Services, which already does the hosting. Left as-is on purpose --
 * de-branding a label should not quietly decide a product question.
 */
const SECTIONS = [
    { id: 'dashboard',      label: 'Dashboard' },
    { id: 'repositories',   label: 'Repositories' },
    { id: 'automations',    label: 'Automations' },
    { id: 'notifications',  label: 'Notifications',       stub: true },
    { id: 'streams',        label: 'Streams',             stub: true },
    { id: 'data-virt',      label: 'API endpoints',       stub: true },
    { id: 'mcp',            label: 'MCP Servers',         stub: true },
    { id: 'apps',           label: 'Apps',                stub: true },
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
        if (section.group) nav.append(el('div', { class: 'nav-group-label' }, 'Admin'));
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

/* Every section in SECTIONS has an entry, so a nav link can never route to
 * nothing. Two kinds of placeholder, and the difference is the point:
 *
 *   stubScreen  -- there is no backend and none is planned in this build.
 *   portPending -- v1 has this screen working; the v2 port has not reached it.
 *
 * Collapsing them into one message would mean a working feature and an absent
 * one reading identically, and the chunk number is the only thing that says
 * which of the two you are looking at.
 */
const SCREENS = {
    dashboard:        [screenDashboard],
    repositories:     [(v) => portPending(v, 'Repositories', 3)],
    jobs:             [(v) => portPending(v, 'Jobs', 5)],
    schedules:        [(v) => portPending(v, 'Schedules', 6)],
    automations:      [(v) => portPending(v, 'Automations', 7)],
    connections:      [(v) => portPending(v, 'Connections', 8)],
    services:         [(v) => portPending(v, 'Services', 9)],
    admin:            [(v) => portPending(v, 'Admin', 10)],
    // stub sections — visible in the nav, no backend
    notifications:    [(v) => stubScreen(v, 'Notifications')],
    streams:          [(v) => stubScreen(v, 'Streams')],
    'data-virt':      [(v) => stubScreen(v, 'API endpoints')],
    mcp:              [(v) => stubScreen(v, 'MCP Servers')],
    apps:             [(v) => stubScreen(v, 'Apps')],
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
//
// That detachment is also why mountTable() passes the container to table.js
// explicitly: a detached node has no `.view` ancestor to find.
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

/* Sections that exist in FME Flow but have no Datum-Sync backend. An honest
 * empty state, not an empty table: a table with no rows looks exactly like a
 * backend that is broken. */
function stubScreen(view, label) {
    view.append(
        el('h1', {}, label),
        el('div', { class: 'stub-empty' },
            el('h2', {}, 'Not yet available'),
            el('p', {}, 'This section is not part of the current Datum-Sync build. '
                + 'Working sections: Repositories, Jobs, Schedules, Automations, '
                + 'Connections, and Services.')));
}

/* Sections that work in v1 and are waiting on their port chunk. Names the
 * chunk, and points at the v1 UI, because "not yet available" would be false
 * here -- the feature exists, this copy of the UI has not caught up. */
function portPending(view, label, chunk) {
    view.append(
        el('h1', {}, label),
        el('div', { class: 'stub-empty' },
            el('h2', {}, 'Not ported yet'),
            el('p', {}, `This screen is chunk ${chunk} of the v2 port. It works `
                + 'today in the current UI.'),
            el('p', {}, el('a', { href: '/ui' }, 'Open it in the current UI'))));
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

/* v2's badge carries a glyph; v1's was text only.
 *
 * Only the four job states the mockups draw are mapped. `cancelled`, and the
 * `enabled`/`paused` badges the schedule screens use, get no glyph rather than
 * a nearly-right one -- the closest candidates in icons.js are `remove` (a
 * trash can) and `warn` (a hazard triangle), and either would say something
 * about a cancelled job that is not true. A badge with no icon reads as a
 * badge; a badge with the wrong icon reads as a different status.
 */
const BADGE_GLYPHS = {
    complete: 'ok',
    failed: 'fail',
    running: 'run',
    queued: 'pending',
};

function badge(status) {
    const glyph = BADGE_GLYPHS[status];
    return el('span', { class: 'badge ' + status },
        glyph ? icon(glyph, 13) : null, glyph ? ' ' : null, status);
}

function banner(err) {
    return el('div', { class: 'banner' },
        el('b', {}, (err.code || 'ERROR') + ': '), err.message || String(err));
}

// ---------------------------------------------------------------------------
// list chrome
// ---------------------------------------------------------------------------

/* Flow's list-page header: the title, then one row carrying a search field on
 * the left and the action buttons packed against the right. v1 called this
 * listbar() and emitted `.listbar`; v2's stylesheet has no such class, and the
 * markup is `.action-bar`.
 *
 * The search input is NOT wired here, and that is the difference from v1.
 * table.js binds it in mountTable(), because searching, sorting and paging are
 * one problem -- a filter that hides rows without telling the pager reports
 * "Showing 1 to 12 of 12" over three visible rows. v1 could get away with
 * hiding rows in place because it had no pager that meant anything.
 *
 * `actions` is an array of button/link nodes in Flow's left-to-right order
 * (create first, destructive last). A button that needs a selection declares
 * it with data-needs="one"|"many" and table.js enables it; declaring it in the
 * markup rather than matching on the label means renaming a button cannot
 * silently unwire it.
 */
function actionBar(view, title, opts) {
    const o = opts || {};
    if (title) view.append(el('h1', {}, title));
    if (o.subtitle) view.append(el('p', { class: 'subtitle' }, o.subtitle));

    const bar = el('div', { class: 'action-bar' });

    if (o.search) {
        bar.append(el('div', { class: 'search' }, el('input', {
            type: 'search',
            placeholder: o.search,
            'aria-label': o.search,
        })));
    }

    if (o.actions && o.actions.length) {
        bar.append(el('div', { class: 'actions' },
            // Filled by table.js as the selection changes; present from the
            // start so it does not shift the buttons when it appears.
            el('span', { class: 'sel-count', 'aria-live': 'polite' }),
            o.actions,
            el('button', {
                type: 'button', class: 'secondary icon', 'aria-label': 'Choose columns',
            }, icon('columns', 16))));
    }

    // A bar with neither a search nor an action is just a margin. Screens that
    // drop both on some paths (the connection create form) would otherwise
    // leave a gap under the title with nothing in it.
    if (bar.firstChild) view.append(bar);
    return bar;
}

/* A toolbar button. `needs` is 'one' or 'many' for the ones that act on a
 * selection; they start disabled because at zero selection they are, and
 * table.js is what turns them on. */
function action(label, onclick, opts) {
    const o = opts || {};
    return el('button', {
        type: 'button',
        class: o.primary ? null : 'secondary',
        'data-needs': o.needs || null,
        disabled: o.needs ? true : null,
        onclick,
    }, label);
}

/* The two-line first cell of a v2 list row: a glyph, then a link with a
 * description under it. `.desc` is v2's name for what v1 called `.repo-desc`.
 * Pass no description for a single-line cell -- the dashboard's tables use
 * that form, and the 59-vs-78 row-pitch difference between the two is what
 * check_style.py locks. */
function cellName(glyph, link, desc) {
    return el('div', { class: 'cell-name' }, icon(glyph, 18),
        el('div', {}, link, desc ? el('div', { class: 'desc' }, desc) : null));
}

/* `headings` items are either a string (a plain column) or
 * {label, sortable, sorted, desc}. One column may carry `sorted`: table.js
 * reads it to render in that order first, so the first paint matches what the
 * header claims instead of jumping.
 *
 * The arrow is one glyph for both directions -- style.css rotates it on
 * th.sorted.desc -- which is why table.js never has to build SVG.
 */
function table(headings, rows, opts) {
    const o = opts || {};
    const head = el('tr', {});
    if (o.select) {
        head.append(el('th', { class: 'col-check' },
            el('input', { type: 'checkbox', 'aria-label': 'Select all' })));
    }
    for (const h of headings) {
        const c = typeof h === 'string' ? { label: h } : h;
        if (!c.sortable) { head.append(el('th', {}, c.label)); continue; }
        head.append(el('th', {
            class: 'sortable' + (c.sorted ? ' sorted' : '') + (c.sorted && c.desc ? ' desc' : ''),
            'aria-sort': c.sorted ? (c.desc ? 'descending' : 'ascending') : 'none',
        }, c.label, ' ', el('span', { class: 'sort-arrow' }, icon('sort-asc', 12))));
    }
    return el('table', {}, el('thead', {}, head), el('tbody', {}, rows));
}

/* A row's leading checkbox. The label names the row, so a screen reader
 * announces which one is being selected rather than "checkbox" four times. */
function rowCheck(label) {
    return el('td', { class: 'col-check' },
        el('input', { type: 'checkbox', 'aria-label': 'Select ' + label }));
}

/* The pager. Every value here is what the markup below it currently shows --
 * all `total` rows are in the tbody and there is no second page yet -- and
 * mountTable() then hands it to table.js, which slices to the selected page
 * size and rewrites the hint, the arrows and the number buttons.
 *
 * The four arrows are identified by aria-label, not position: table.js reads
 * them by name. */
function pagerBar(total, perPage) {
    const per = perPage || 100;
    const arrow = (label, glyph) =>
        el('button', { type: 'button', disabled: true, 'aria-label': label }, icon(glyph, 14));
    return el('div', { class: 'pager' },
        el('div', { class: 'pager-btns' },
            arrow('First', 'page-first'),
            arrow('Previous', 'page-prev'),
            el('button', { type: 'button', class: 'cur' }, '1'),
            arrow('Next', 'page-next'),
            arrow('Last', 'page-last')),
        el('div', { class: 'pager-right' },
            el('span', { class: 'hint' }, `Showing 1 to ${total} of ${total} entries`),
            el('div', { class: 'pager-per-page' },
                el('select', { 'aria-label': 'Rows per page' },
                    [10, 25, 50, 100].map((n) =>
                        el('option', { selected: n === per }, n))))));
}

/* Hand a rendered table to table.js. Call this last, after the toolbar, the
 * table and the pager are all in the container: table.js reads the rows once
 * at init and looks the other two up by class.
 *
 * `scope` is the screen's own container, not #view. See the note on
 * `generation` -- a screen that has been navigated away from is detached, and
 * table.js's own fallback would then find the live screen's toolbar instead
 * and quietly wire a dead table to it.
 */
function mountTable(scope) {
    const el_ = scope.querySelector('table');
    if (el_ && window.initTable) window.initTable(el_, scope);
}

/* Horizontal tab strip matching Flow's page-level tab chrome.
 * `tabs` — [{id, label, href}]; `activeId` — the currently active tab id. */
function pageTabs(tabs, activeId) {
    return el('nav', { class: 'page-tabs', 'aria-label': 'Tabs' },
        tabs.map(({ id, label, href }) =>
            el('a', {
                href,
                class: id === activeId ? 'active' : '',
                'aria-current': id === activeId ? 'page' : false,
            }, label)));
}

// ---------------------------------------------------------------------------
// dashboard
// ---------------------------------------------------------------------------

/* The hash segment that means "the create form", not an existing record's id.
 * Shared with the schedule and automation screens when those chunks land. */
const NEW = 'new';

/* Flow's landing page, carrying our data: create tiles across the top, recent
 * items under them, job counters below those, reference links last. A Flow
 * user should not have to look for anything.
 *
 * The counters carry Flow's labels where the two systems name a state
 * differently ('complete' is 'Successful' there). The status in the link is
 * ours, because that is what /transformations/jobs filters on.
 */
const COUNTERS = [
    // Flow's grouping too: the three settled states on one row, the two live
    // ones on a wider row under them.
    [['failed', 'Failed'], ['complete', 'Successful'], ['cancelled', 'Cancelled']],
    [['queued', 'Queued'], ['running', 'Running']],
];

/* The ring: one arc per settled outcome, sized by its share of the settled
 * total. r=70 on a 160 viewBox, so the circumference is 2*pi*70 and every
 * dasharray below is a fraction of it. style.css rotates the svg -90deg, so
 * the first segment starts at twelve o'clock.
 *
 * PORT.md left this open -- the mockup draws a ring the backend does not
 * serve, and warned against shipping one fed by invented numbers. It does not
 * need any: /transformations/jobs/summary already returns every count, and the
 * mockup's own figures turn out to be exactly these three counts scaled to the
 * circumference. So it is computed, not faked, and it is drawn from the same
 * response the counters below it use -- the ring and the counters cannot
 * disagree, because there is only one number for each.
 *
 * Returns null when nothing has settled. A ring of three zero-length arcs is
 * an empty grey circle with "0" in it, which reads as a chart that failed to
 * load rather than as an instance where nothing has finished yet.
 */
function ringChart(counts) {
    const segments = ['complete', 'failed', 'cancelled'].map((s) => [s, counts[s] || 0]);
    const total = segments.reduce((sum, [, n]) => sum + n, 0);
    if (!total) return null;

    const circumference = 2 * Math.PI * 70;
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('viewBox', '0 0 160 160');
    svg.setAttribute('role', 'img');
    // The only description of the ring a screen reader gets, so it carries the
    // numbers rather than the word "chart".
    svg.setAttribute('aria-label', segments.map(([s, n]) => `${n} ${s}`).join(', '));

    let offset = 0;
    for (const [status, n] of segments) {
        const length = (n / total) * circumference;
        const arc = document.createElementNS(SVG, 'circle');
        arc.setAttribute('class', status);
        arc.setAttribute('cx', '80');
        arc.setAttribute('cy', '80');
        arc.setAttribute('r', '70');
        arc.setAttribute('fill', 'none');
        // currentColor, so .ring-chart .complete etc. colour it. A stroke:
        // rule would be the odd one out -- every other svg here is coloured
        // through the text colour, and without those rules the whole ring
        // inherits --text and paints one solid near-black arc.
        arc.setAttribute('stroke', 'currentColor');
        arc.setAttribute('stroke-width', '14');
        arc.setAttribute('stroke-dasharray', `${length.toFixed(2)} ${circumference.toFixed(2)}`);
        arc.setAttribute('stroke-dashoffset', (-offset).toFixed(2));
        svg.append(arc);
        offset += length;
    }

    return el('div', { class: 'ring-chart' }, svg,
        el('div', { class: 'center' },
            el('span', { class: 'n' }, total),
            el('span', { class: 'label' }, 'Completed')));
}

async function screenDashboard(view) {
    // Scopes the h2 rule to this screen; elsewhere an h2 is a panel title.
    view.className = 'dashboard';
    view.append(el('h1', {}, 'Dashboard'));

    // Create tiles. The mockup points the first one at #/run, which is not a
    // section -- these use the routes that exist.
    const tiles = [
        ['Run Workspace', 'repositories', '#/repositories'],
        ['Create Schedule', 'schedules', '#/schedules/' + NEW],
        ['Create Automation', 'automations', '#/automations/' + NEW],
        // Creating a connection is admin-only at the API. Offering it here
        // unconditionally would put a form nobody may submit one click away.
        me.is_admin && ['Create Connection', 'connections', '#/connections?new=1'],
    ].filter(Boolean);
    view.append(el('div', { class: 'tiles' }, tiles.map(([label, glyph, href]) =>
        el('a', { class: 'tile', href }, icon(glyph, 18), el('span', {}, label)))));

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
                icon('repositories', 18),
                el('span', { class: 'ws-name' }, repo.name),
                el('span', { class: 'ws-meta' },
                    repo.workspaces, ' workspace', repo.workspaces === 1 ? '' : 's')))));
    }

    // Recent jobs. Single-line rows -- no cellName here. That is the 59-vs-78
    // row pitch check_style.py locks: the dashboard's tables are a glance, the
    // list screens' are a working surface.
    //
    // Not handed to mountTable(): five rows with no toolbar and no pager have
    // nothing for table.js to sort, select or page, and wiring it would give
    // this table a search box the mockup does not have.
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

    // Right rail.
    const ring = ringChart(counts);
    if (ring) {
        rail.append(el('div', { class: 'rail-card' },
            el('h3', {}, 'Job outcomes'), ring));
    }

    // One reference card, not the mockup's two. Its second points at
    // #/resources and promises "Engines, drivers and disk"; that section is a
    // stubScreen with no backend, so the card would be a claim about content
    // the screen behind it then contradicts. Same reason the ring is computed
    // rather than drawn from the mockup's numbers.
    rail.append(el('div', { class: 'rail-card' },
        el('h3', {}, 'Reference'),
        el('a', { class: 'link-card', href: '/docs', target: '_blank', rel: 'noopener' },
            el('span', { class: 'lc-text' },
                el('b', {}, 'REST API'),
                el('span', {}, 'Every route this page calls, with its schema')),
            el('span', { class: 'lc-arrow' }, '\u2192'))));

    // Repoll only while something is moving, and hand back the cancel. Without
    // it the timer outlives the screen and calls route() from whatever page
    // the user navigated to.
    if (counts.queued || counts.running) {
        const timer = setTimeout(route, 4000);
        return () => clearTimeout(timer);
    }
}

// ---------------------------------------------------------------------------

start();
