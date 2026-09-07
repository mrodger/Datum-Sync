/* FME Flow UI reference screenshots — 21 pages, alphabetical. */
const PAGES = [
  'analytics', 'auth-services', 'automations', 'connections', 'dashboard',
  'data-virtualization', 'flow-apps', 'jobs', 'mcp', 'migration',
  'notifications', 'projects', 'queue-control', 'repositories', 'resources',
  'run-workspace', 'schedules', 'security', 'streams', 'system-config',
  'workspaces'
];

const IMAGES = PAGES.map(name => ({
  id: name,
  name: name,
  src: name + '.png',
  label: titleCase(name),
}));

/* State: { [id]: { starred, note } } */
const state = {};
let currentFilter = 'all';
let lbIndex = -1;
let visibleImages = [];

// Persist in localStorage keyed to this gallery
const STORAGE_KEY = 'datum-sync-fme-reference';
try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) Object.assign(state, JSON.parse(saved));
} catch(e) {}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}
function getState(id) { return state[id] || { starred: false, note: '' }; }
function setState(id, patch) {
  state[id] = { ...getState(id), ...patch };
  saveState();
}

function titleCase(s) {
  return s.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function counts() {
  let starred = 0, annotated = 0;
  for (const img of IMAGES) {
    const s = getState(img.id);
    if (s.starred) starred++;
    if (s.note && s.note.trim()) annotated++;
  }
  return { total: IMAGES.length, starred, annotated };
}

function updateCounts() {
  const c = counts();
  document.getElementById('countAll').textContent = c.total;
  document.getElementById('countStarred').textContent = c.starred;
  document.getElementById('countAnnotated').textContent = c.annotated;
  document.getElementById('statTotal').textContent = c.total;
  document.getElementById('statStarred').textContent = c.starred;
  document.getElementById('statAnnotated').textContent = c.annotated;
}

// Sidebar page nav
function buildPageNav() {
  const nav = document.getElementById('pageNav');
  nav.innerHTML = '';
  for (const img of IMAGES) {
    const item = document.createElement('div');
    item.className = 'nav-item';
    item.dataset.page = img.id;
    item.onclick = () => scrollToCard(img.id);
    const s = getState(img.id);
    item.innerHTML = `<span>${img.label}</span>` +
      (s.starred ? `<span class="count count-star">&#9733;</span>` : '');
    nav.appendChild(item);
  }
}

function scrollToCard(id) {
  // Switch to all view if not visible
  if (currentFilter !== 'all') setFilter('all', document.querySelector('[data-filter="all"]'));
  const el = document.getElementById('card-' + id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function setFilter(f, el) {
  currentFilter = f;
  document.querySelectorAll('.sidebar .nav-item').forEach(n => {
    if (n.dataset.filter) n.classList.toggle('active', n.dataset.filter === f);
  });
  const titles = { all: 'All Pages', starred: 'Starred', annotated: 'Annotated' };
  document.getElementById('viewTitle').textContent = titles[f] || 'All Pages';
  render();
}

function render() {
  const gal = document.getElementById('gallery');
  gal.innerHTML = '';

  visibleImages = IMAGES.filter(img => {
    const s = getState(img.id);
    if (currentFilter === 'starred') return s.starred;
    if (currentFilter === 'annotated') return s.note && s.note.trim();
    return true;
  });

  if (visibleImages.length === 0) {
    gal.innerHTML = '<div class="empty"><h3>No images match</h3><p>Try a different filter.</p></div>';
    updateCounts();
    return;
  }

  visibleImages.forEach((img, idx) => {
    const s = getState(img.id);
    const card = document.createElement('div');
    card.className = 'card' + (s.starred ? ' starred' : '');
    card.id = 'card-' + img.id;
    card.innerHTML = `
      <div class="img-wrap" data-action="lightbox" data-idx="${idx}">
        <img src="${img.src}" alt="${img.label}" loading="lazy">
        <span class="badge">${img.label}</span>
      </div>
      <div class="meta">
        <div class="top-row">
          <span class="name">${img.label}</span>
          <button class="star-btn ${s.starred ? 'on' : ''}"
            data-action="star" data-id="${img.id}">${s.starred ? '&#9733;' : '&#9734;'}</button>
        </div>
        <textarea placeholder="Add annotation..."
          data-action="note" data-id="${img.id}">${s.note || ''}</textarea>
      </div>`;
    gal.appendChild(card);
  });

  updateCounts();
}

function toggleStar(id) {
  const s = getState(id);
  setState(id, { starred: !s.starred });
  render();
  buildPageNav();
}

function updateNote(id, val) {
  setState(id, { note: val });
  updateCounts();
}

// Lightbox
function openLightbox(idx) {
  lbIndex = idx;
  updateLightbox();
  document.getElementById('lightbox').classList.add('open');
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}
function lbNav(dir) {
  lbIndex = (lbIndex + dir + visibleImages.length) % visibleImages.length;
  updateLightbox();
}
function updateLightbox() {
  const img = visibleImages[lbIndex];
  const s = getState(img.id);
  document.getElementById('lbImg').src = img.src;
  document.getElementById('lbCaption').textContent = img.label;
  document.getElementById('lbNote').textContent = s.note || '';
}
document.addEventListener('keydown', e => {
  const lb = document.getElementById('lightbox');
  if (!lb.classList.contains('open')) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowLeft') lbNav(-1);
  if (e.key === 'ArrowRight') lbNav(1);
  if (e.key === 's') {
    const img = visibleImages[lbIndex];
    toggleStar(img.id);
    updateLightbox();
  }
});

// Export / import
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}

function exportAnnotations() {
  const data = { exported: new Date().toISOString(), annotations: state };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fme-reference-annotations.json';
  a.click();
  toast('Exported annotations');
}

function importAnnotations() {
  document.getElementById('importInput').click();
}
function handleImport(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      const ann = data.annotations || data;
      Object.assign(state, ann);
      saveState();
      render();
      buildPageNav();
      toast('Imported ' + Object.keys(ann).length + ' annotations');
    } catch(err) { toast('Import failed: ' + err.message); }
  };
  reader.readAsText(file);
  e.target.value = '';
}

function exportMarkdown() {
  const starred = IMAGES.filter(img => getState(img.id).starred);
  if (!starred.length) { toast('No starred images'); return; }
  let md = '# FME Flow UI Reference — Selected Pages\n\n';
  md += 'Exported: ' + new Date().toISOString().slice(0, 10) + '\n\n';
  md += starred.length + ' of ' + IMAGES.length + ' selected.\n\n';
  starred.forEach(img => {
    const s = getState(img.id);
    md += '## ' + img.label + '\n';
    md += '- **File:** `' + img.src + '`\n';
    if (s.note) md += '- **Notes:** ' + s.note + '\n';
    md += '\n';
  });
  const blob = new Blob([md], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'fme-reference-picks.md';
  a.click();
  toast('Exported ' + starred.length + ' picks');
}

// Init
buildPageNav();
render();

/* Event delegation.
 *
 * Every handler used to be an `onclick="..."` attribute. The hosted-service CSP
 * forbids inline script, and an inline handler IS inline script -- the page
 * loaded and rendered with every control dead. Delegation from document also
 * survives `render()` replacing the gallery's innerHTML, which per-node
 * listeners would not.
 */
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el) return;
  switch (el.dataset.action) {
    case 'filter':      setFilter(el.dataset.filter, el); break;
    case 'import':      importAnnotations(); break;
    case 'export-json': exportAnnotations(); break;
    case 'export-md':   exportMarkdown(); break;
    case 'lb-close':    closeLightbox(); break;
    case 'lb-prev':     lbNav(-1); break;
    case 'lb-next':     lbNav(1); break;
    case 'lightbox':    openLightbox(Number(el.dataset.idx)); break;
    case 'star':        toggleStar(el.dataset.id); break;
  }
});

document.addEventListener('input', (e) => {
  const el = e.target.closest('[data-action="note"]');
  if (el) updateNote(el.dataset.id, el.value);
});

document.getElementById('importInput')
  .addEventListener('change', (e) => handleImport(e));
