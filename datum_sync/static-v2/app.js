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
    repositories:     [screenRepositories, screenRepository, screenWorkspace],
    jobs:             [screenJobs, screenJob],
    schedules:        [screenSchedules, screenSchedule],
    automations:      [screenAutomations, screenAutomation],
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
    // `.page-desc`, not v1's `.subtitle`. Both are styled, so either would have
    // looked deliberate; the mockups settle it -- schedules, automations and
    // connections all write the sentence under a list title as `.page-desc`,
    // and no mockup uses `.subtitle` at all. This option had no callers until
    // chunk 6, so the wrong class had never reached a screen.
    if (o.desc) view.append(el('p', { class: 'page-desc' }, o.desc));

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
 * table.js is what turns them on.
 *
 * `off` is for a button with no backend behind it at all, and it is NOT the
 * same as `needs` with nothing selected. table.js assigns `btn.disabled` from
 * the selection count on every change (table.js 164-167), so a `data-needs`
 * button is enabled the moment a row is ticked, whatever it was built with.
 * A button that must never be clickable therefore has to carry no `needs` --
 * otherwise it lights up and does nothing, which reads as a broken feature
 * rather than an absent one.
 */
function action(label, onclick, opts) {
    const o = opts || {};
    return el('button', {
        type: 'button',
        class: o.primary ? null : 'secondary',
        'data-needs': o.off ? null : (o.needs || null),
        disabled: o.off || o.needs ? true : null,
        title: o.title || null,
        onclick: o.off ? null : onclick,
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
 *
 * Returns table.js's handle, whose `selection()` gives the ticked rows' keys --
 * a key being the row's index in the array the table was built from. Only the
 * jobs list needs it so far; every other caller ignores it.
 */
function mountTable(scope) {
    const el_ = scope.querySelector('table');
    if (!el_ || !window.initTable) return null;
    return window.initTable(el_, scope) || null;
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
// repositories
// ---------------------------------------------------------------------------

/* Publishing is a CLI operation. There is no POST, PUT or DELETE anywhere
 * under /rest/v1/repositories -- `repos` on the server is the only way in --
 * so all four toolbar buttons are `off` rather than wired or `needs`-gated.
 * The mockup draws Create and Upload live and Edit/Remove as selection-gated;
 * drawn that way here, ticking a row would enable Edit and clicking it would
 * do nothing. The empty state below says where the operation actually lives.
 */
async function screenRepositories(view) {
    const { items } = await api('/repositories');

    if (!items.length) {
        view.append(el('h1', {}, 'Repositories'));
        // .empty-state, not .empty: style.css calls the latter "legacy ...
        // for inline empties", and this is the whole screen. h3, not the
        // connections mockup's h2 -- style.css only sizes `.empty-state h3`,
        // so that mockup's heading renders at 1.375rem where the sheet
        // designed 1rem.
        view.append(el('div', { class: 'empty-state' },
            el('div', { class: 'es-icon' }, icon('repositories', 48)),
            el('h3', {}, 'Nothing published'),
            el('p', {}, 'Use the ', el('code', {}, 'repos'),
                ' CLI on the server to publish a repository. There is no '
                + 'endpoint for it, so it cannot be done from here.')));
        return;
    }

    actionBar(view, 'Repositories', {
        search: 'Search repositories by name',
        actions: [
            action('Create', null, { primary: true, off: true, title: 'Use the repos CLI' }),
            action('Upload', null, { off: true, title: 'Use the repos CLI' }),
            action('Edit', null, { off: true, title: 'Use the repos CLI' }),
            action('Remove', null, { off: true, title: 'Use the repos CLI' }),
        ],
    });

    /* Two columns, where the mockup has four. The two it loses are the two
     * the endpoint cannot fill:
     *
     * OWNER does not exist. The repositories table has `name` and `path` and
     * nothing else; v1 renders the literal string 'admin' in every row. A
     * column that is the same invented word all the way down is not a fact
     * about a repository, and porting it would carry the invention forward.
     *
     * WORKSPACES exists, but as the second line of the name cell rather than
     * as its own column. The count is the only other thing this response
     * carries, and the v2 list row wants a description under the name -- with
     * a column too it would be the same number printed twice on one row.
     *
     * `path` is left off deliberately: it is an absolute server path, and the
     * list is the one screen every reader of every repository sees.
     *
     * The mockup's trailing "open" cell goes with them, and for a reason only
     * the screenshot showed: it is the same href as the name beside it, and
     * with nothing between the two columns the browser splits the row down
     * the middle and strands it there. In the mockup it sits at the right
     * edge because Owner and Workspaces fill the space -- take those away and
     * a duplicate link floats in the gap they left.
     */
    const rows = items.map((repo) => el('tr', {},
        rowCheck(repo.name),
        el('td', {}, cellName('repositories',
            el('a', { href: '#/repositories/' + encodeURIComponent(repo.name) }, repo.name),
            repo.workspaces + ' workspace' + (repo.workspaces === 1 ? '' : 's')))));

    // No hand-wired select-all here. v1 syncs the header checkbox to the rows
    // itself; from mountTable() on, that is table.js's, and both running would
    // toggle each row twice.
    view.append(table([{ label: 'Repository', sortable: true, sorted: true }],
        rows, { select: true }));
    view.append(pagerBar(items.length));
    mountTable(view);
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
// run a workspace
// ---------------------------------------------------------------------------

/* One control for one published parameter, and its read().
 *
 * read() returns null for "the user left this alone", which readParams() turns
 * into an absent key so the manifest default applies -- not into an empty
 * string, which would override the default with nothing.
 */
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

/* The mockup writes the required marker as `<span class="req">*</span>` inside
 * the label, and style.css only reaches it there (`.field .req`). A checkbox
 * puts the input before the label; everything else after it.
 */
function field(param, ctl) {
    const label = el('label', { for: 'param-' + param.name },
        param.name, param.required ? el('span', { class: 'req' }, ' *') : null);
    const hint = el('div', { class: 'hint' },
        param.description || param.type.toLowerCase().replace('_', ' '));
    return param.type === 'BOOLEAN'
        ? el('div', { class: 'field' }, el('div', { class: 'check' }, ctl.input, label), hint)
        : el('div', { class: 'field' }, label, ctl.input, hint);
}

/* Read a set of controls into the params object submit takes.
 *
 * The FILE branch keys off `instanceof File` rather than off `ctl.file`, so
 * that the schedule forms in chunk 6 can hand back an upload id they already
 * hold and have it pass through untouched.
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

/* Group the parameter fields into cards, but only if the manifest grouped all
 * of them.
 *
 * `group` is a display-only field -- nothing in the backend reads it, it exists
 * so a UI can lay the form out the way the workspace author meant. v1 ignored
 * it and rendered one flat list. The mockup's card idiom is the shape for it,
 * so v2 uses it.
 *
 * Partial grouping falls back to one card. A manifest where some parameters
 * name a group and some do not has not decided, and honouring it would file
 * some parameters under a heading its author wrote and the rest under one
 * invented here.
 */
function parameterCards(controls) {
    const grouped = controls.length && controls.every(({ param }) => param.group);
    if (!grouped) {
        return [el('div', { class: 'card' },
            el('h2', {}, 'Published Parameters'),
            controls.map(({ param, ctl }) => field(param, ctl)))];
    }
    const order = [];
    const byGroup = new Map();
    for (const { param, ctl } of controls) {
        if (!byGroup.has(param.group)) { order.push(param.group); byGroup.set(param.group, []); }
        byGroup.get(param.group).push(field(param, ctl));
    }
    return order.map((label) => el('div', { class: 'card' },
        el('h2', {}, label), byGroup.get(label)));
}

/* The run form.
 *
 * The mockup is drawn as a standalone launcher: a Workspace card holding three
 * selects -- Repository, Workspace, Service -- above the parameters. None of
 * the three survives, and none of them for the same reason as chunk 3's
 * columns.
 *
 * Repository and Workspace are in the URL. This screen is only reachable at
 * #/repositories/{repo}/{workspace}, so by the time it renders they are chosen.
 * A select offering to change them is either inert or a navigation control
 * wearing a form control's clothes, and the crumbs above already do that job.
 *
 * Service has nothing behind it. POST /transformations/submit/{repo}/{ws} takes
 * `params` and an idempotency key; there is no service argument. `services` on
 * the manifest is the list of interfaces the workspace enables, not a choice
 * made per run -- offering it as a select would let somebody pick one and
 * watch it be dropped.
 *
 * So the card keeps the heading and states the same three facts read-only,
 * alongside the rest of the manifest. Also dropped: the header's "Workspace
 * Actions" caret button. chunk 3's `off` is for a button that is visibly
 * present and dead, which is right for a toolbar of four where one day some
 * will work; a lone caret that opens no menu is just a broken menu.
 */
async function screenWorkspace(view, repo, name) {
    const ws = await api(
        `/repositories/${encodeURIComponent(repo)}/workspaces/${encodeURIComponent(name)}`);

    // Filtered by the server, not here: asking for the newest 10 jobs and
    // keeping this workspace's would show nothing whenever ten other jobs ran
    // more recently.
    const { items: recent } = await api('/transformations/jobs?limit=10'
        + `&repository=${encodeURIComponent(repo)}&workspace=${encodeURIComponent(name)}`);

    const controls = ws.parameters.map((p) => ({ param: p, ctl: control(p) }));
    const status = el('div', {});
    const run = el('button', { type: 'submit' }, icon('run', 15), ' Run');

    const form = el('form', {},
        el('div', { class: 'card' },
            el('h2', {}, 'Workspace'),
            el('dl', { class: 'kv' },
                el('dt', {}, 'Repository'), el('dd', {}, repo),
                el('dt', {}, 'Version'), el('dd', {}, ws.version),
                el('dt', {}, 'Timeout'), el('dd', {}, ws.timeout_seconds, 's'),
                el('dt', {}, 'Services'), el('dd', {}, ws.services.join(', ') || '\u2014'),
                el('dt', {}, 'Outputs'),
                el('dd', {}, ws.outputs.map((o) => o.name).join(', ') || '\u2014'),
                el('dt', {}, 'Connections'),
                el('dd', {}, ws.connections.map((c) => c.name).join(', ') || '\u2014'))),
        controls.length
            ? parameterCards(controls)
            : el('div', { class: 'card' },
                el('h2', {}, 'Published Parameters'),
                el('p', { class: 'subtitle' }, 'This workspace publishes no parameters.')),
        status,
        el('div', { class: 'action-bar' },
            el('div', { class: 'actions' },
                run,
                action('Cancel', () => go('#/repositories/' + encodeURIComponent(repo))))));

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

    view.append(
        crumbs(['Repositories', '#/repositories'],
               [repo, '#/repositories/' + encodeURIComponent(repo)],
               [name]),
        el('h1', {}, name),
        el('p', { class: 'subtitle' }, ws.description || 'No description.'),
        form,
        el('h2', {}, 'Recent runs'),
        recent.length
            ? table(['Status', 'Submitted', 'By', ''],
                recent.map((job) => el('tr', {},
                    el('td', {}, badge(job.status)),
                    el('td', {}, when(job.submitted_at)),
                    el('td', {}, job.submitted_by),
                    el('td', {}, el('a', { href: '#/jobs/' + job.id }, 'open')))))
            : el('div', { class: 'empty' }, 'This workspace has not been run recently.'));
}

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------

/* A job that will never move again. Used twice: to decide whether the list
 * repolls, and to decide whether the detail screen offers Resubmit or Cancel.
 * v1 has the same constant for the same two reasons. */
const TERMINAL = ['complete', 'failed', 'cancelled'];

/* Flow's Jobs tabs. The mockup's fourth is "Dashboards", which is a Flow
 * feature and not a job status -- as a filter it would either 400 (it is not
 * in JOB_STATUSES) or silently show everything. v1's "All" is kept instead:
 * same position, same shape, and it means something here.
 */
const JOB_TABS = [
    { id: 'complete', label: 'Completed', href: '#/jobs?status=complete' },
    { id: 'queued',   label: 'Queued',    href: '#/jobs?status=queued'   },
    { id: 'running',  label: 'Running',   href: '#/jobs?status=running'  },
    { id: '',         label: 'All',       href: '#/jobs'                 },
];

async function screenJobs(view) {
    const filter = hashQuery().get('status') || '';
    const query = filter ? '?status=' + encodeURIComponent(filter) : '';
    const { items } = await api('/transformations/jobs' + query);

    view.append(el('h1', {}, 'Jobs'));
    view.append(pageTabs(JOB_TABS, filter));

    /* Cancel is wired; Remove is not, and the difference is that one of them
     * has a route. DELETE on a job cancels it -- there is nothing anywhere
     * under /transformations/jobs that deletes the row -- so a working Remove
     * would either be a second Cancel under a name that promises more, or a
     * button that reports success and leaves the row there. `off`, per the
     * note on action(): a `needs` button lights up the moment a row is ticked,
     * whoever built it disabled.
     *
     * "Run Workspace" is the mockup's primary and goes where the run form
     * chunk 4 built lives. It cannot go straight to the form -- that URL names
     * a repository and a workspace, and this screen knows neither -- so it
     * goes to the list you pick them from.
     */
    const bar = actionBar(view, null, {
        search: 'Search jobs by workspace or user',
        actions: [
            action('Run Workspace', () => go('#/repositories'), { primary: true }),
            action('Cancel', () => cancelSelected(), { needs: 'many' }),
            action('Remove', null, { off: true, title: 'Jobs are cancelled, not deleted' }),
        ],
    });

    if (!items.length) {
        // The bar stays: its search and its Run Workspace button are still the
        // right things to offer, and a page with only a heading on it reads as
        // a screen that failed rather than a queue that is empty.
        view.append(el('div', { class: 'empty' },
            'No jobs', filter ? ' with this status.' : ' yet.'));
        return;
    }

    const rows = items.map((job) => el('tr', {},
        rowCheck('job ' + job.id.slice(0, 8)),
        // Short id, because a job id here is a UUID and the mockup's column is
        // sized for Flow's four-digit integers. Eight hex characters is what
        // the crumb on the detail screen already shows, so the two agree.
        el('td', {}, el('a', { href: '#/jobs/' + job.id }, job.id.slice(0, 8))),
        el('td', {}, badge(job.status)),
        // The workspace cell links to the WORKSPACE, not to the job -- that is
        // the mockup's href and it is the useful one, since the job is one
        // click away in the first column and its run form is not reachable
        // from anywhere else on this page.
        el('td', {}, cellName('workspaces',
            el('a', {
                href: '#/repositories/' + encodeURIComponent(job.repository)
                    + '/' + encodeURIComponent(job.workspace),
            }, job.workspace),
            job.repository)),
        el('td', {}, job.submitted_by),
        el('td', {}, duration(job.started_at, job.completed_at))));

    /* No column starts sorted, where the mockup starts on Job descending.
     * Flow's job ids are integers and sorting them backwards is "newest
     * first"; ours are UUIDs, so the same sort is alphabetical over random
     * hex -- an arbitrary order presented as a meaningful one. Left unsorted,
     * table.js preserves the order the response arrived in, and the endpoint
     * already returns `ORDER BY submitted_at DESC`. The rows are newest-first
     * either way; only this way is it true.
     *
     * Duration is not sortable for the same reason in miniature: the cell is
     * text from duration(), so "2m 5s" sorts before "30s".
     */
    view.append(table([
        'Job',
        { label: 'Status', sortable: true },
        { label: 'Workspace', sortable: true },
        { label: 'Requested by', sortable: true },
        'Duration',
    ], rows, { select: true }));
    view.append(pagerBar(items.length));
    const handle = mountTable(view);

    /* Cancel every ticked job, then re-route to show what happened.
     *
     * Terminal jobs are not filtered out. jobs.cancel() reads the row and
     * returns its status untouched when there is nothing to stop, so sending
     * them is safe; filtering here would mean writing a second copy of the
     * "can this still be cancelled?" rule in the client, where it would be one
     * status name away from disagreeing with the server's.
     */
    async function cancelSelected() {
        if (!handle) return;
        const chosen = handle.selection().map((key) => items[Number(key)]).filter(Boolean);
        await Promise.all(chosen.map((job) =>
            api('/transformations/jobs/id/' + encodeURIComponent(job.id), { method: 'DELETE' })));
        route();
    }

    // Only while something is moving. A finished queue is not repolled, and
    // the timer is handed back so it does not outlive the screen and call
    // route() from whatever page the reader navigated to.
    if (items.some((j) => j.status === 'queued' || j.status === 'running')) {
        /* Skipped while rows are ticked. v1 could repoll unconditionally
         * because its jobs list had nothing to lose; this one does. route()
         * rebuilds the screen, which builds a new table, which starts with an
         * empty selection -- so a reader who ticks four rows and reaches for
         * Cancel has under four seconds to get there, and beats it or does not
         * depending on when they arrived. Worse, the failure is invisible:
         * the tick marks vanish at the same moment the rows are redrawn, so it
         * reads as the page refreshing rather than as their selection being
         * thrown away.
         *
         * And it lands precisely where it does the most damage. A list only
         * repolls when something on it is queued or running, which is to say
         * on the Queued and Running tabs -- the two where Cancel is the reason
         * you are on the page at all.
         *
         * Waiting is the right resolution rather than merely the easy one: a
         * reader with a selection has stopped watching the queue and started
         * acting on it, and the rows they ticked are by definition rows they
         * have already seen. Nothing is missed, only deferred, and it resumes
         * by itself the moment the selection is cleared or spent.
         */
        let timer = null;
        const poll = () => {
            if (handle && handle.selection().length) {
                timer = setTimeout(poll, 4000);
                return;
            }
            route();
        };
        timer = setTimeout(poll, 4000);
        return () => clearTimeout(timer);
    }
}

async function screenJob(view, id) {
    const job = await api('/transformations/jobs/id/' + encodeURIComponent(id));

    const statusCell = el('dd', {}, badge(job.status));
    // Held, not inlined: a status arriving over SSE moves these too. Rendering
    // them once from the first fetch left a job reading COMPLETE with no finish
    // time, forever, which is how this was found in v1.
    const startedCell = el('dd', {}, when(job.started_at));
    const finishedCell = el('dd', {}, when(job.completed_at));
    const artifacts = el('dd', {});
    const log = el('div', { class: 'log' });

    /* Progress is announced but never stored -- datum_sync/jobs.py says why --
     * so there is nothing to draw this from on load. It appears when the first
     * report arrives and is absent again after a reload mid-run. Hidden until
     * then rather than shown at 0%, which would claim knowledge of a job that
     * may report nothing at all.
     *
     * It sits inside the Log panel, which is the mockup's placement and not
     * v1's. Progress is a summary of the same stream the log below it is
     * printing, and in the Details panel it read as another property of the
     * job, next to Submitted and Artifacts, rather than as the thing moving.
     */
    const progressFill = el('div', { class: 'fill', style: 'width:0%' });
    const progressText = el('span', {});
    const progressPct = el('span', {});
    const progress = el('div', { class: 'progress', hidden: true },
        el('div', { class: 'track' }, progressFill),
        el('div', { class: 'caption' }, progressText, progressPct));

    // v2 puts the actions in the page header beside the h1, where the mockup
    // has them, rather than under the details list.
    const actions = el('div', { class: 'actions' });

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

    /* One button, not the mockup's two -- and that is not a divergence:
     * tools/gen_mockups.py's own comment on screen_job() says both are drawn
     * only to pin their geometry, and that "showActions() swaps them on the
     * terminal states". A job is either still going or it is not, and offering
     * Cancel on a finished one is an offer that cannot be honoured.
     */
    function showActions(status) {
        clear(actions);
        if (TERMINAL.includes(status)) {
            actions.append(el('button', {
                type: 'button',
                class: 'secondary',
                onclick: async () => {
                    const next = await api(
                        `/transformations/jobs/id/${encodeURIComponent(job.id)}/resubmit`,
                        { method: 'POST' });
                    go('#/jobs/' + next.id);
                },
            }, icon('refresh', 15), ' Resubmit'));
        } else {
            actions.append(el('button', {
                type: 'button',
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

    /* Where the mockup has "Engine". There is no engine column on the jobs
     * table and no engine field in the response -- the mockup's "engine-2" is
     * an invented value, and porting it would mean printing a made-up worker
     * name on a page people read to find out what actually ran.
     *
     * `triggered_by` and `parent_job` are real, are returned, and were shown
     * nowhere in v1. api.py calls triggered_by the thing "the UI's 'why did
     * this run?'" reads, so this is that place: a job that a schedule or an
     * automation started says so, and a resubmission links back to the run it
     * came from. Both are absent on a job somebody submitted by hand, and the
     * rows are omitted rather than dashed -- an em-dash next to "Triggered by"
     * invites the question of what the missing trigger was.
     */
    const kv = el('dl', { class: 'kv' },
        el('dt', {}, 'Status'), statusCell,
        el('dt', {}, 'Workspace'),
        el('dd', {}, el('a', {
            href: '#/repositories/' + encodeURIComponent(job.repository)
                + '/' + encodeURIComponent(job.workspace),
        }, job.repository + '/' + job.workspace)),
        el('dt', {}, 'Requested by'), el('dd', {}, job.submitted_by),
        job.triggered_by ? el('dt', {}, 'Triggered by') : null,
        job.triggered_by ? el('dd', {}, job.triggered_by) : null,
        job.parent_job ? el('dt', {}, 'Resubmitted from') : null,
        job.parent_job
            ? el('dd', {}, el('a', { href: '#/jobs/' + job.parent_job },
                String(job.parent_job).slice(0, 8)))
            : null,
        el('dt', {}, 'Submitted'), el('dd', {}, when(job.submitted_at)),
        el('dt', {}, 'Started'), startedCell,
        el('dt', {}, 'Finished'), finishedCell,
        /* min-width:0 is not decoration, and it is not a style.css edit by the
         * back door. `.kv dd` already declares `overflow-wrap: break-word` --
         * the designer's answer to a long value is "wrap it" -- but the rule
         * cannot fire here: `.kv` is a grid, a grid item's default min-width is
         * `auto` (= min-content), and overflow-wrap does not shrink min-content.
         * So the track widens to fit whichever value is longest and the panel
         * overflows instead of the text wrapping. Measured on the live page:
         * this dd was 497px inside a 369px panel, and 234px with min-width:0.
         *
         * Only this row carries it because only this row holds something the
         * user did not write: every other value is a name, a timestamp or a
         * short id, while params is a machine-serialised blob whose length is
         * unbounded. Setting it on the grid would change every kv on every
         * screen to fix one of them.
         */
        el('dt', {}, 'Parameters'),
        el('dd', { style: 'min-width:0' }, JSON.stringify(job.params)),
        el('dt', {}, 'Artifacts'), artifacts);

    view.append(
        crumbs(['Jobs', '#/jobs'], [job.id.slice(0, 8)]),
        el('div', { class: 'page-header' },
            el('h1', {}, 'Job ', job.id.slice(0, 8)),
            actions),
        error,
        el('div', { class: 'split' },
            el('div', { class: 'panel' }, el('h2', {}, 'Log'), progress, log),
            el('div', { class: 'panel' }, el('h2', {}, 'Details'), kv)));

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
        // Appended with nothing between the elements: .log is white-space:
        // pre-wrap, so a text node carrying a newline would paint as a blank
        // line between every pair of entries. mock-job.html says the same.
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

function every(seconds) {
    for (const [unit, size] of [['d', 86400], ['h', 3600], ['m', 60]]) {
        if (seconds % size === 0) return (seconds / size) + unit;
    }
    return seconds + 's';
}

/* The Trigger cell. The mockup sets both forms in `<code>` -- cron and
 * "every 1h" alike -- so the column reads as one kind of thing, and v2 follows
 * it there.
 *
 * It does not follow the mockup in dropping the timezone. `0 2 * * *` is not a
 * time until you know the zone it is read in, and the zone is a stored,
 * editable field of the schedule: showing the expression without it states
 * two thirds of the answer in a column whose whole job is to say when this
 * runs. An interval carries no zone because it is not evaluated in one.
 */
function triggerOf(schedule) {
    return schedule.cron
        ? el('span', {}, el('code', {}, schedule.cron), ' ',
             el('span', { class: 'hint' }, schedule.timezone))
        : el('code', {}, 'every ', every(schedule.interval_s));
}

/* "in 6 hours", the way the mockup writes Next run -- and "2 minutes ago", the
 * way it writes Last fired. The sign of the difference picks the direction, so
 * one function serves both columns; it was called untilNode until chunk 7
 * needed the backwards half.
 *
 * The absolute time goes in the title, and not as a nicety: a relative time is
 * computed once and then sits there, and this list has no repoll to correct
 * it, so a tab left open overnight reads "in 6 hours" about a run that
 * happened. Hovering gives the timestamp that is still true. Intl does the
 * wording, so it is the browser's locale rather than a table of English
 * plurals maintained here.
 */
function relativeNode(iso) {
    if (!iso) return el('span', {}, '\u2014');
    const ms = new Date(iso) - new Date();
    const units = [['day', 86400000], ['hour', 3600000], ['minute', 60000], ['second', 1000]];
    let text;
    try {
        const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
        const [unit, size] = units.find(([, s]) => Math.abs(ms) >= s) || units[3];
        text = rtf.format(Math.round(ms / size), unit);
    } catch (e) {
        text = when(iso);
    }
    return el('span', { title: when(iso) }, text);
}

/* The trigger half of a schedule form: cron or interval, and the zone the cron
 * is read in. Returned as {node, read} for the same reason control() is, so a
 * trigger cannot be rendered as one kind and read as another. */
function triggerFields(schedule) {
    const isCron = !schedule || schedule.cron !== null;
    const zone = (schedule && schedule.timezone) || 'Pacific/Auckland';

    // Intl ships the zone list, so there is no bundled table to go stale and
    // no third-party fetch. The current value is prepended if this browser
    // does not know it, so editing a schedule can never silently retime it.
    let all;
    try { all = Intl.supportedValuesOf('timeZone'); } catch (e) { all = ['UTC']; }
    if (!all.includes(zone)) all = [zone, ...all];

    const kind = el('select', { id: 'trigger-kind' },
        el('option', { value: 'cron', selected: isCron }, 'Cron expression'),
        el('option', { value: 'interval', selected: !isCron }, 'Fixed interval'));
    const cron = el('input', {
        type: 'text', id: 'trigger-cron', placeholder: '0 7 * * 1-5',
        value: (schedule && schedule.cron) || '',
    });
    const seconds = el('input', {
        type: 'number', id: 'trigger-interval', min: '1',
        value: (schedule && schedule.interval_s) || 900,
    });
    const zoneInput = el('select', { id: 'trigger-zone' },
        all.map((z) => el('option', { value: z, selected: z === zone }, z)));

    const wrap = (id, label, input, hint) => el('div', { class: 'field' },
        el('label', { for: id }, label), input, el('div', { class: 'hint' }, hint));

    const cronField = wrap('trigger-cron', 'Cron expression', cron,
        'Five fields: minute hour day month weekday.');
    const intervalField = wrap('trigger-interval', 'Interval (seconds)', seconds,
        'A duration, so it does not shift when the clocks do.');
    const zoneField = wrap('trigger-zone', 'Timezone', zoneInput,
        '07:00 here stays 07:00 across a daylight saving change.');

    function show() {
        const cronNow = kind.value === 'cron';
        cronField.hidden = !cronNow;
        intervalField.hidden = cronNow;
        // An interval is not evaluated in a zone, so offering one would suggest
        // it changes something. It is still sent and still stored.
        zoneField.hidden = !cronNow;
    }
    kind.addEventListener('change', show);
    show();

    return {
        node: el('div', { class: 'card' },
            el('h2', {}, 'Trigger'),
            wrap('trigger-kind', 'Trigger', kind, 'How the next run time is decided.'),
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
            // reads as null. On an edit form that would quietly drop the upload
            // the schedule has been running with for weeks, and the next run
            // would fail on a missing required parameter. Hold the stored id
            // and return it unless a new file is actually chosen. readParams()
            // keys its upload branch off `instanceof File`, so an id handed
            // back this way passes through untouched.
            const held = stored[p.name];
            const chosen = ctl.read;
            ctl.read = () => chosen() || held;
        }
        return { param: seeded, ctl };
    });
}

async function screenSchedules(view) {
    const { items } = await api('/schedules');

    /* Create is a link dressed as a button, not a button with go() behind it.
     * The mockup draws a <button>, but this one navigates: as an <a href> it
     * gets middle-click, ctrl-click and the status-bar preview for free, and
     * the toolbar in style.css sizes `.button` to match. Pause, Edit and Remove
     * are real buttons because they act.
     *
     * All three selection actions have an endpoint behind them -- PATCH for
     * enabled, DELETE for removal -- which is why none of them is `off` the way
     * chunk 3's publish buttons are.
     */
    actionBar(view, 'Schedules', {
        desc: 'A schedule runs one workspace on a cron expression or a fixed '
            + 'interval. Pausing one stops it firing without discarding it.',
        search: 'Search schedules by name or workspace',
        actions: [
            el('a', { class: 'button', href: '#/schedules/' + NEW }, 'Create'),
            action('Pause', () => pauseSelected(), { needs: 'many' }),
            action('Edit', () => editSelected(), { needs: 'one' }),
            action('Remove', () => removeSelected(), { needs: 'many' }),
        ],
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty-state' },
            el('div', { class: 'es-icon' }, icon('schedules', 48)),
            el('h3', {}, 'Nothing scheduled'),
            el('p', {}, 'A schedule runs one workspace on a cron expression or '
                + 'a fixed interval.')));
        return;
    }

    view.append(table([
        { label: 'Status', sortable: true },
        { label: 'Name', sortable: true, sorted: true },
        { label: 'Trigger', sortable: true },
        { label: 'Next run', sortable: true },
        { label: 'Last job', sortable: true },
    ], items.map((s) => el('tr', {},
        rowCheck(s.name),
        el('td', {}, badge(s.enabled ? 'enabled' : 'paused')),
        el('td', {}, cellName('schedules',
            el('a', { href: '#/schedules/' + s.id }, s.name),
            s.repository + '/' + s.workspace)),
        el('td', {}, triggerOf(s)),
        // Only for an enabled schedule. Pausing does not clear next_run, so a
        // paused one still carries whatever time it was paused at -- shown,
        // that reads as permanently overdue, which it is not.
        el('td', {}, s.enabled ? relativeNode(s.next_run) : '\u2014'),
        el('td', {}, s.last_job
            ? el('a', { href: '#/jobs/' + s.last_job }, s.last_job.slice(0, 8))
            : '\u2014'))), { select: true }));

    view.append(pagerBar(items.length));
    const handle = mountTable(view);

    const chosen = () => (handle
        ? handle.selection().map((key) => items[Number(key)]).filter(Boolean)
        : []);

    /* Pause sets enabled=false; it does not toggle each row to its opposite.
     *
     * The button says Pause, and over a mixed selection a toggle would resume
     * the paused ones -- the reader would have pressed a button labelled Pause
     * and started something. Setting the state named on the button makes the
     * paused rows a no-op, which is the outcome somebody pressing Pause
     * expects. Resuming stays on the detail screen, where there is one
     * schedule and the button can say which way it goes.
     */
    async function pauseSelected() {
        await Promise.all(chosen().map((s) =>
            api('/schedules/' + s.id, { method: 'PATCH', json: { enabled: false } })));
        route();
    }

    function editSelected() {
        const [s] = chosen();
        if (s) go('#/schedules/' + s.id);
    }

    async function removeSelected() {
        const picked = chosen();
        if (!picked.length) return;
        const names = picked.map((s) => s.name).join(', ');
        // The only confirm() in the UI, and deliberately the only one. Cancel
        // is reversible -- resubmit the job -- and pausing is reversible by
        // definition. A schedule is a row nothing else stores: delete it and
        // the cron expression, the timezone and the parameter set it has been
        // running with are gone, with no undo anywhere in the API.
        if (!window.confirm(`Delete ${picked.length} schedule(s)?\n\n${names}`)) return;
        await Promise.all(picked.map((s) =>
            api('/schedules/' + s.id, { method: 'DELETE' })));
        route();
    }
}

async function screenSchedule(view, id) {
    return id === NEW ? newSchedule(view) : editSchedule(view, id);
}

async function newSchedule(view) {
    const { items: repos } = await api('/repositories');

    const name = el('input', {
        type: 'text', id: 'sched-name', required: true, placeholder: 'nightly-export' });
    const repo = el('select', { id: 'sched-repo', required: true },
        el('option', { value: '' }, 'Choose a repository\u2026'),
        repos.map((r) => el('option', { value: r.name }, r.name)));
    const workspace = el('select', { id: 'sched-workspace', required: true, disabled: true },
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
        append(params, [parameterCards(controls)]);
    });

    const fieldOf = (id, label, input, hint) => el('div', { class: 'field' },
        el('label', { for: id }, label), input, el('div', { class: 'hint' }, hint));

    const form = el('form', {},
        el('div', { class: 'card' },
            el('h2', {}, 'Schedule'),
            fieldOf('sched-name', 'Name', name,
                'Unique, and how the schedule signs the jobs it submits.'),
            fieldOf('sched-repo', 'Repository', repo,
                'Cannot be changed later: a schedule that could be repointed is '
                + 'a permission check made once, on a row that no longer says '
                + 'what it said.'),
            fieldOf('sched-workspace', 'Workspace', workspace,
                'Its published parameters appear below once chosen.')),
        trigger.node,
        params,
        status,
        // Chunk 4's form footer, not a new class: `.form-actions` does not
        // exist in style.css, and an unstyled div would have looked deliberate.
        el('div', { class: 'action-bar' },
            el('div', { class: 'actions' },
                create,
                action('Cancel', () => go('#/schedules')))));

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        create.disabled = true;
        clear(status);
        try {
            const body = Object.assign({
                name: name.value.trim(),
                repository: repo.value,
                workspace: workspace.value,
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
        el('p', { class: 'page-desc' },
            'The workspace and its parameters are checked now, so a schedule '
            + 'that could never run is refused here rather than at 3am.'),
        form);
}

async function editSchedule(view, id) {
    const s = await api('/schedules/' + encodeURIComponent(id));

    // A schedule can outlive the workspace it points at -- unpublishing does
    // not delete schedules -- and that is exactly when someone comes to look at
    // it. So a 404 here degrades to a read-only view of the stored params
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

    const form = el('form', {},
        trigger.node,
        parameters
            ? parameterCards(controls)
            : el('div', { class: 'card' },
                el('h2', {}, 'Published Parameters'),
                el('div', { class: 'banner' },
                    s.repository, '/', s.workspace, ' is no longer published. ',
                    'The stored parameters are shown but cannot be edited here.'),
                el('pre', { class: 'mono' }, JSON.stringify(s.params, null, 2))),
        el('div', { class: 'action-bar' },
            el('div', { class: 'actions' },
                save,
                action('Cancel', () => go('#/schedules')))),
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

    // Pause/Resume names the direction because there is one schedule here and
    // its state is known -- unlike the list's Pause, which acts on a mixed set.
    const toggle = el('button', {
        type: 'button', class: 'secondary',
        onclick: async () => {
            toggle.disabled = true;
            await api('/schedules/' + encodeURIComponent(id),
                { method: 'PATCH', json: { enabled: !s.enabled } });
            route();
        },
    }, icon('refresh', 15), ' ', s.enabled ? 'Pause' : 'Resume');

    const remove = el('button', {
        type: 'button', class: 'danger',
        onclick: async () => {
            if (!window.confirm(`Delete the schedule ${s.name}?`)) return;
            remove.disabled = true;
            await api('/schedules/' + encodeURIComponent(id), { method: 'DELETE' });
            go('#/schedules');
        },
    }, 'Delete');

    view.append(
        crumbs(['Schedules', '#/schedules'], [s.name]),
        el('div', { class: 'page-header' },
            el('h1', {}, s.name),
            el('div', { class: 'actions' }, badge(s.enabled ? 'enabled' : 'paused'),
                toggle, remove)),
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
                    el('dd', {}, s.enabled ? relativeNode(s.next_run) : 'paused'),
                    el('dt', {}, 'Last run'), el('dd', {}, when(s.last_run)),
                    el('dt', {}, 'Last job'),
                    el('dd', {}, s.last_job
                        ? el('a', { href: '#/jobs/' + s.last_job }, s.last_job.slice(0, 8))
                        : '\u2014'),
                    el('dt', {}, 'Created by'), el('dd', {}, s.created_by || '\u2014'),
                    el('dt', {}, 'Created'), el('dd', {}, when(s.created_at))),
                el('p', { class: 'hint' },
                    'Repository and workspace cannot be changed. A schedule that '
                    + 'could be repointed is a permission check made once, on a '
                    + 'row that no longer says what it said.'))));
}

// ---------------------------------------------------------------------------
// automations
// ---------------------------------------------------------------------------

/* The document a new automation starts from. Deliberately complete and
 * deliberately not runnable as-is: every field is shown with a real value so
 * the shape is learnable from the form, and REPOSITORY/WORKSPACE are obvious
 * placeholders so nobody saves the example by accident and wonders why it
 * never fires. Carried over from v1 unchanged -- it is the server's vocabulary
 * written out, not a piece of v1 chrome. */
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

/* The mockup writes action types as English -- "run workspace", "webhook" --
 * where v1 printed the stored `run_workspace` / `http_request`. That is a
 * second vocabulary living in the browser, and the failure it invites is
 * silent: a third action type added in automations.py would render as blank or
 * `undefined` here, and no test of either side alone would notice.
 *
 * So the fallback is not a dash and not an empty string. An unrecognised type
 * comes out as its own name with the underscores opened up, which is wrong-ish
 * English but is never nothing -- the row still says what it does. The pairing
 * with the server's ACTIONS tuple is what test_v2_labels_every_automation_action
 * holds; this map is allowed to be incomplete only in the direction that still
 * renders.
 */
const ACTION_LABELS = {
    run_workspace: 'run workspace',
    http_request: 'webhook',
};

function actionLabel(action) {
    return ACTION_LABELS[action.type] || String(action.type).replace(/_/g, ' ');
}

function actionsOf(config) {
    return (config.actions || []).map(actionLabel).join(', ');
}

/* The `.desc` line under the name, in the mockup's two halves: what fires it,
 * then what it watches. "job complete &middot; SCIMAC/site_plan".
 *
 * v1 gave this its own Trigger column; the mockup folds it into the name cell,
 * which is why the v2 table has one column fewer than v1's.
 *
 * A trigger with no status matches every terminal status, failures included --
 * so it is spelled out rather than left blank, because a blank there reads as
 * "complete" to anyone who has only ever seen the other rows.
 */
function triggerDesc(config) {
    const t = config.trigger || {};
    const fires = t.status ? 'job ' + t.status : 'any finished job';
    let watches;
    if (!t.repository) watches = 'any repository';
    else if (!t.workspace) watches = 'any workspace in ' + t.repository;
    else watches = t.repository + '/' + t.workspace;
    return fires + ' \u00b7 ' + watches;
}

async function screenAutomations(view) {
    const { items } = await api('/automations');

    actionBar(view, 'Automations', {
        desc: 'An automation watches for finished jobs and runs a workspace or '
            + 'calls a URL when one matches.',
        search: 'Search automations by name',
        actions: [
            el('a', { class: 'button', href: '#/automations/' + NEW }, 'Create'),
            action('Pause', () => pauseSelected(), { needs: 'many' }),
            action('Edit', () => editSelected(), { needs: 'one' }),
            action('Remove', () => removeSelected(), { needs: 'many' }),
        ],
    });

    if (!items.length) {
        view.append(el('div', { class: 'empty-state' },
            el('div', { class: 'es-icon' }, icon('automations', 48)),
            el('h3', {}, 'No automations'),
            el('p', {}, 'An automation watches for finished jobs and runs a '
                + 'workspace or calls a URL when one matches.')));
        return;
    }

    view.append(table([
        { label: 'Status', sortable: true },
        { label: 'Name', sortable: true, sorted: true },
        { label: 'Actions', sortable: true },
        { label: 'Last fired', sortable: true },
        { label: 'Last error', sortable: true },
    ], items.map((a) => el('tr', {},
        rowCheck(a.name),
        el('td', {}, badge(a.enabled ? 'enabled' : 'paused')),
        el('td', {}, cellName('automations',
            el('a', { href: '#/automations/' + a.id }, a.name),
            triggerDesc(a.config))),
        el('td', {}, actionsOf(a.config)),
        // Unlike a schedule's next run, last_fired is a fact about the past: a
        // paused automation still fired when it fired, so it is shown either
        // way.
        el('td', {}, relativeNode(a.last_fired)),
        el('td', { class: 'error' }, a.last_error || ''))), { select: true }));

    view.append(pagerBar(items.length));
    const handle = mountTable(view);

    const chosen = () => (handle
        ? handle.selection().map((key) => items[Number(key)]).filter(Boolean)
        : []);

    // Sets enabled=false rather than toggling each row, for the reason set out
    // on the schedules list: a mixed selection under a button labelled Pause
    // must not start anything.
    async function pauseSelected() {
        await Promise.all(chosen().map((a) =>
            api('/automations/' + a.id, { method: 'PATCH', json: { enabled: false } })));
        route();
    }

    function editSelected() {
        const [a] = chosen();
        if (a) go('#/automations/' + a.id);
    }

    async function removeSelected() {
        const picked = chosen();
        if (!picked.length) return;
        const names = picked.map((a) => a.name).join(', ');
        // The second confirm() in the UI, for the same reason as the first: the
        // YAML document is stored nowhere else, and the API has no undelete.
        if (!window.confirm(`Delete ${picked.length} automation(s)?\n\n${names}`)) return;
        await Promise.all(picked.map((a) =>
            api('/automations/' + a.id, { method: 'DELETE' })));
        route();
    }
}

async function screenAutomation(view, id) {
    return id === NEW ? newAutomation(view) : editAutomation(view, id);
}

/* The editor, shared by both forms.
 *
 * It holds the stored YAML verbatim -- not the parsed config re-serialised. An
 * editor that hands back a normalised document silently discards comments, key
 * order and quoting style, so opening an automation and saving it unchanged
 * would rewrite it.
 *
 * Nothing here parses YAML. The server does, and it is the only thing that
 * does, so the editor can neither accept a document the server would refuse
 * nor refuse one it would accept.
 */
function yamlEditor(text) {
    return el('textarea', { class: 'yaml', spellcheck: 'false', rows: 22 }, text);
}

function definitionCard(editor) {
    return el('div', { class: 'card' }, el('h2', {}, 'Definition'), editor);
}

async function newAutomation(view) {
    const editor = yamlEditor(AUTOMATION_TEMPLATE);
    const status = el('div', {});
    const create = el('button', { type: 'submit' }, 'Create automation');

    const form = el('form', {},
        definitionCard(editor),
        el('div', { class: 'action-bar' },
            el('div', { class: 'actions' },
                create,
                action('Cancel', () => go('#/automations')))),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        create.disabled = true;
        clear(status);
        try {
            await api('/automations', { method: 'POST', json: { yaml: editor.value } });
            go('#/automations');
        } catch (err) {
            status.append(banner(err));
        } finally {
            create.disabled = false;
        }
    });

    view.append(
        crumbs(['Automations', '#/automations'], ['New']),
        el('h1', {}, 'New automation'),
        el('p', { class: 'page-desc' },
            'Placeholders are ', el('code', {}, '{{job.id}}'), ', ',
            el('code', {}, '{{job.status}}'), ' and ',
            el('code', {}, '{{params.NAME}}'), '. A name outside that set is '
            + 'refused now rather than posted as literal text later.'),
        form);
}

async function editAutomation(view, id) {
    const a = await api('/automations/' + encodeURIComponent(id));
    const { items: runs } = await api(
        '/automations/' + encodeURIComponent(id) + '/runs');

    const editor = yamlEditor(a.yaml);
    const status = el('div', {});
    const save = el('button', { type: 'submit' }, 'Save');

    const form = el('form', {},
        definitionCard(editor),
        el('div', { class: 'action-bar' },
            el('div', { class: 'actions' },
                save,
                action('Cancel', () => go('#/automations')))),
        status);

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        clear(status);
        try {
            // PUT, not PATCH: this replaces the whole document, including the
            // name and the enabled flag it carries. PATCH on this resource
            // takes only `enabled` and is what the toggle below uses.
            await api('/automations/' + encodeURIComponent(id),
                { method: 'PUT', json: { yaml: editor.value } });
            go('#/automations');
        } catch (err) {
            status.append(banner(err));
        } finally {
            save.disabled = false;
        }
    });

    const toggle = el('button', {
        type: 'button', class: 'secondary',
        onclick: async () => {
            toggle.disabled = true;
            await api('/automations/' + encodeURIComponent(id),
                { method: 'PATCH', json: { enabled: !a.enabled } });
            route();
        },
    }, icon('refresh', 15), ' ', a.enabled ? 'Pause' : 'Resume');

    const remove = el('button', {
        type: 'button', class: 'danger',
        onclick: async () => {
            if (!window.confirm(`Delete the automation ${a.name}?`)) return;
            remove.disabled = true;
            await api('/automations/' + encodeURIComponent(id), { method: 'DELETE' });
            go('#/automations');
        },
    }, 'Delete');

    // append(), not view.append(). The banner below is conditional, and
    // Node.append is the DOM's, which stringifies whatever it is handed: a null
    // child renders as the four characters "null" on the page. Our append()
    // drops null, undefined and false, which is why el() can take a conditional
    // child and this cannot. It printed a blue "null" under the title for the
    // length of one screenshot.
    append(view, [
        crumbs(['Automations', '#/automations'], [a.name]),
        el('div', { class: 'page-header' },
            el('h1', {}, a.name),
            el('div', { class: 'actions' }, badge(a.enabled ? 'enabled' : 'paused'),
                toggle, remove)),
        // The last error sits above the editor, not in a column: this is the
        // screen someone opens *because* the list showed one, and it is the
        // document below that has to change to clear it.
        a.last_error ? el('div', { class: 'banner' }, a.last_error) : null,
        el('div', { class: 'split' },
            form,
            el('div', { class: 'panel' },
                el('h2', {}, 'Details'),
                el('dl', { class: 'kv' },
                    el('dt', {}, 'Trigger'), el('dd', {}, triggerDesc(a.config)),
                    el('dt', {}, 'Actions'), el('dd', {}, actionsOf(a.config)),
                    el('dt', {}, 'Last fired'), el('dd', {}, relativeNode(a.last_fired)),
                    el('dt', {}, 'Created by'), el('dd', {}, a.created_by || '\u2014'),
                    el('dt', {}, 'Created'), el('dd', {}, when(a.created_at)),
                    el('dt', {}, 'Updated'), el('dd', {}, when(a.updated_at))),
                el('p', { class: 'hint' },
                    'The name, the trigger and the enabled flag all live in the '
                    + 'document. Saving replaces it whole.'))),
        el('h2', {}, 'Runs'),
        runs.length
            ? table(['Result', 'Fired', 'Triggered by', 'Detail'],
                runs.map((r) => el('tr', {},
                    el('td', {}, badge(r.ok ? 'complete' : 'failed')),
                    el('td', {}, when(r.fired_at)),
                    el('td', {}, r.trigger_job
                        ? el('a', { href: '#/jobs/' + r.trigger_job },
                            r.trigger_job.slice(0, 8))
                        : '\u2014'),
                    el('td', {}, el('span', { class: 'mono' },
                        JSON.stringify(r.results))))))
            : el('div', { class: 'empty-state' },
                el('div', { class: 'es-icon' }, icon('automations', 48)),
                el('h3', {}, 'Not fired yet'),
                el('p', {}, 'Runs appear here once a job matches the trigger.')),
    ]);
}

// ---------------------------------------------------------------------------

start();
