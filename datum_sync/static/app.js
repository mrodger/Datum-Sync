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
    repositories: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    jobs: 'M4 6h16M4 12h16M4 18h10',
    schedules: 'M5 5h14v14H5zM5 9h14M9 3v4M15 3v4',
    automations: 'M13 3 5 14h6l-2 7 8-11h-6z',
    connections: 'M9 7V4M15 7V4M7 7h10v5a5 5 0 0 1-10 0zM12 17v4',
    resources: 'M6 3h7l5 5v13H6zM13 3v5h5',
    services: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c5 6 5 12 0 18-5-6-5-12 0-18z',
    admin: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
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
        me.is_admin ? el('span', { class: 'badge-admin' }, 'admin') : null,
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

const SECTIONS = [
    { id: 'repositories', label: 'Repositories' },
    { id: 'jobs', label: 'Jobs' },
    { id: 'schedules', label: 'Schedules' },
    { id: 'automations', label: 'Automations' },
    { id: 'connections', label: 'Connections' },
    { id: 'resources', label: 'Resources' },
    { id: 'services', label: 'Services' },
    { id: 'admin', label: 'Admin', adminOnly: true },
];

function buildNav() {
    const nav = clear($('nav'));
    for (const section of SECTIONS) {
        // Hidden, not disabled -- and hiding is presentation only. Every route
        // behind Admin checks is_admin for itself; this just declutters.
        if (section.adminOnly && !me.is_admin) continue;
        nav.append(el('a', {
            href: '#/' + section.id,
            id: 'nav-' + section.id,
        }, icon(section.id), el('span', { class: 'nav-label' }, section.label)));
    }
}

function hashQuery() {
    return new URLSearchParams((location.hash.split('?')[1]) || '');
}

function parseHash() {
    // The query string lives inside the hash (`#/jobs?status=running`), so it
    // has to come off before splitting on '/' -- otherwise the section name is
    // "jobs?status=running" and matches no screen.
    const raw = (location.hash || '#/repositories').split('?')[0].replace(/^#\/?/, '');
    return raw.split('/').filter(Boolean).map(decodeURIComponent);
}

const SCREENS = {
    repositories: [screenRepositories, screenRepository, screenWorkspace],
    jobs: [screenJobs, screenJob],
    schedules: [(view) => notBuilt(view, 'Schedules', 7)],
    automations: [(view) => notBuilt(view, 'Automations', 7)],
    connections: [(view) => notBuilt(view, 'Connections', 8)],
    resources: [(view) => notBuilt(view, 'Resources', null)],
    services: [(view) => notBuilt(view, 'Services', 10)],
    admin: [screenAdmin],
};

let leaveScreen = null;

async function route() {
    if (!me) return;
    // Whatever the last screen left running -- an SSE subscription, a poll --
    // stops before the next one starts. Without this, navigating away from a
    // job detail leaves its EventSource open for the life of the tab.
    if (leaveScreen) { leaveScreen(); leaveScreen = null; }

    const parts = parseHash();
    const [section, ...rest] = parts.length ? parts : ['repositories'];
    for (const link of $('nav').children) {
        link.classList.toggle('active', link.id === 'nav-' + section);
    }

    const screens = SCREENS[section];
    const view = clear($('view'));
    if (!screens) return void view.append(notBuiltNode('Not found', 'No such screen.'));

    const screen = screens[Math.min(rest.length, screens.length - 1)];
    try {
        leaveScreen = (await screen(view, ...rest)) || null;
    } catch (err) {
        if (err instanceof ApiError && err.status === 401) return showSignin();
        view.append(el('div', { class: 'banner' },
            el('b', {}, (err.code || 'ERROR') + ': '), err.message || String(err)));
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

function table(headings, rows) {
    return el('table', {},
        el('thead', {}, el('tr', {}, headings.map((h) => el('th', {}, h)))),
        el('tbody', {}, rows));
}

// ---------------------------------------------------------------------------
// repositories
// ---------------------------------------------------------------------------

async function screenRepositories(view) {
    const { items } = await api('/repositories');
    view.append(el('h1', {}, 'Repositories'));
    if (!items.length) {
        view.append(el('div', { class: 'empty' },
            'Nothing published. Use the ', el('code', {}, 'repos'),
            ' CLI to add a repository.'));
        return;
    }
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
            const params = {};
            for (const { param, ctl } of controls) {
                const value = ctl.read();
                if (value === null || value === undefined) {
                    if (param.required) throw new ApiError(0, 'INVALID_PARAMETER',
                        `${param.name} is required`);
                    continue;   // absent, so the manifest default applies
                }
                // A FILE parameter carries an upload id, never a path or the
                // bytes: the file goes up first and the job gets the id back.
                params[param.name] = ctl.file ? await upload(value) : value;
            }
            const job = await api(
                `/transformations/submit/${encodeURIComponent(repo)}/${encodeURIComponent(name)}`,
                { method: 'POST', json: { params } });
            go('#/jobs/' + job.id);
        } catch (err) {
            status.append(el('div', { class: 'banner' },
                el('b', {}, (err.code || 'ERROR') + ': '), err.message || String(err)));
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

    const select = el('select', { style: 'width:auto' },
        el('option', { value: '', selected: !filter }, 'All statuses'),
        STATUSES.map((s) => el('option', { value: s, selected: s === filter }, s)));
    // Redraws via hashchange. Calling route() here as well would render twice,
    // the first time against the hash the browser has not updated yet.
    select.addEventListener('change', () =>
        go(select.value ? '#/jobs?status=' + select.value : '#/jobs'));

    view.append(
        el('div', { style: 'display:flex;justify-content:space-between;align-items:center' },
            el('h1', {}, 'Jobs'), select));

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
