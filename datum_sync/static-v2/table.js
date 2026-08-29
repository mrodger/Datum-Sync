/* table.js — sort, selection and paging for a Flow-shaped list table.
 *
 * Shared by every mockup, and written so static-v2/app.js can adopt it
 * unchanged. It drives a table that is already in the DOM: the rows are the
 * model, and nothing here fetches or knows about the API.
 *
 * The three behaviours are one module because they are one problem. Sorting
 * and paging both reorder what is on screen, filtering changes what exists at
 * all, and a selection has to mean the same thing after each of them --
 * otherwise Remove deletes a row the user never saw. Built as three separate
 * widgets they would each be correct alone and wrong together.
 *
 * Four decisions worth stating, because they are not all forced:
 *
 *  1. A row's identity is its ORIGINAL index, baked into data-row-key once at
 *     init. Position cannot be the key -- that is the thing sorting changes.
 *
 *  2. Selection survives sorting and paging, and is CLEARED by the filter.
 *     Sort and page still show you the same result set, so a selection off
 *     screen is only out of view. A filter removes rows from the set, and a
 *     selection you can no longer reach by scrolling is a Remove aimed at
 *     something invisible. Clearing is the safe reading.
 *
 *  3. Select-all covers the CURRENT PAGE, not the whole set, and goes
 *     indeterminate when the page is partly selected. A box that silently
 *     picks up 400 off-screen rows is the same hazard as (2).
 *
 *  4. Edit needs exactly one row, Remove needs one or more. Flow's screenshots
 *     only show both disabled at zero selection; the rest is our decision, not
 *     an observation of Flow.
 */
(function () {
    'use strict';

    function textOf(row, col) {
        var cell = row.children[col];
        return cell ? cell.textContent.trim() : '';
    }

    /* Numeric when every value in the column parses as a number. Job ids and
       workspace counts sort 2 < 10 that way; names still sort as text, and a
       column mixing "12.4s" with an em-dash placeholder stays textual rather
       than sorting the dash as zero. */
    function comparator(rows, col) {
        var numeric = rows.every(function (r) {
            var v = textOf(r, col);
            return v !== '' && !isNaN(Number(v));
        });
        if (numeric) {
            return function (a, b) { return Number(textOf(a, col)) - Number(textOf(b, col)); };
        }
        return function (a, b) {
            return textOf(a, col).localeCompare(textOf(b, col), undefined, { numeric: true });
        };
    }

    /* `scope` is where the toolbar and pager that belong to this table are
       looked up. The mockups omit it and the fallback is right for them: one
       table per page, inside the one `.view`.

       app.js must pass it. Its screens render into a container that `route()`
       detaches when you navigate away, and a detached node's closest('.view')
       is null -- which would fall through to `document` and wire a dead
       table's sorting and paging to the LIVE screen's action bar. That is a
       silent cross-screen leak, not an error. */
    function initTable(table, scope) {
        var view = scope || table.closest('.view') || document;
        var body = table.tBodies[0];
        var head = table.tHead.rows[0];
        var bar = view.querySelector('.action-bar');
        var pager = view.querySelector('.pager');
        var searchBox = bar && bar.querySelector('input[type=search]');
        var allBox = head.querySelector('input[type=checkbox]');

        var all = Array.prototype.slice.call(body.rows);
        all.forEach(function (r, i) { r.dataset.rowKey = String(i); });

        var colCount = head.cells.length;
        var selected = new Set();
        var sortCol = -1, sortDir = 1, query = '', page = 1;
        var perPage = Number((pager && pager.querySelector('select') || {}).value) || all.length;

        // The header ships one column already marked sorted, so the first
        // render matches the static mockup instead of jumping on load.
        for (var i = 0; i < head.cells.length; i++) {
            if (head.cells[i].classList.contains('sorted')) {
                sortCol = i;
                sortDir = head.cells[i].classList.contains('desc') ? -1 : 1;
            }
        }

        function matching() {
            if (!query) return all.slice();
            var q = query.toLowerCase();
            return all.filter(function (r) {
                return r.textContent.toLowerCase().indexOf(q) !== -1;
            });
        }

        function render() {
            var rows = matching();
            if (sortCol >= 0) {
                var cmp = comparator(rows, sortCol);
                rows.sort(function (a, b) { return cmp(a, b) * sortDir; });
            }

            var pages = Math.max(1, Math.ceil(rows.length / perPage));
            if (page > pages) page = pages;
            var from = (page - 1) * perPage;
            var shown = rows.slice(from, from + perPage);

            body.textContent = '';
            shown.forEach(function (r) { body.appendChild(r); });

            // An empty tbody reads as a broken page, not as a filter that
            // matched nothing. Say which it is.
            if (!shown.length) {
                var tr = document.createElement('tr');
                var td = document.createElement('td');
                td.colSpan = colCount;
                td.className = 'empty-row';
                td.textContent = query ? 'No rows match "' + query + '".' : 'Nothing to show.';
                tr.appendChild(td);
                body.appendChild(tr);
            }

            shown.forEach(function (r) {
                var cb = r.querySelector('input[type=checkbox]');
                if (cb) cb.checked = selected.has(r.dataset.rowKey);
            });

            syncHeader(shown);
            syncSortArrows();
            syncActions();
            syncPager(rows.length, pages, from, shown.length);
        }

        function syncHeader(shown) {
            if (!allBox) return;
            var n = shown.filter(function (r) {
                return selected.has(r.dataset.rowKey);
            }).length;
            allBox.checked = shown.length > 0 && n === shown.length;
            allBox.indeterminate = n > 0 && n < shown.length;
        }

        function syncSortArrows() {
            for (var i = 0; i < head.cells.length; i++) {
                var th = head.cells[i];
                if (!th.classList.contains('sortable')) continue;
                th.classList.toggle('sorted', i === sortCol);
                th.classList.toggle('desc', i === sortCol && sortDir === -1);
                th.setAttribute('aria-sort', i !== sortCol ? 'none'
                    : (sortDir === 1 ? 'ascending' : 'descending'));
            }
        }

        /* The toolbar buttons declare their own requirement in the markup
           (data-needs="one" | "many") rather than being matched by label here.
           Renaming a button in the generator should not silently unwire it. */
        function syncActions() {
            if (!bar) return;
            var n = selected.size;
            bar.querySelectorAll('[data-needs]').forEach(function (btn) {
                var need = btn.dataset.needs;
                btn.disabled = need === 'one' ? n !== 1 : n < 1;
            });
            var hint = bar.querySelector('.sel-count');
            if (hint) {
                hint.textContent = n ? n + ' selected' : '';
            }
        }

        function syncPager(total, pages, from, count) {
            if (!pager) return;
            var hint = pager.querySelector('.hint');
            if (hint) {
                hint.textContent = total
                    ? 'Showing ' + (from + 1) + ' to ' + (from + count) +
                      ' of ' + total + ' entries'
                    : 'No entries';
            }

            var btns = pager.querySelector('.pager-btns');
            if (!btns) return;
            var kids = btns.children;
            kids[0].disabled = kids[1].disabled = page === 1;
            kids[kids.length - 2].disabled = kids[kids.length - 1].disabled = page === pages;

            // Rebuild the number buttons between the prev/next pairs. With one
            // page this leaves a single "1", which is what the static markup
            // showed, so a short list looks unchanged.
            while (kids.length > 4) btns.removeChild(kids[2]);
            var before = kids[2];
            for (var p = 1; p <= pages; p++) {
                var b = document.createElement('button');
                b.type = 'button';
                b.textContent = String(p);
                if (p === page) b.className = 'cur';
                b.dataset.page = String(p);
                btns.insertBefore(b, before);
            }
        }

        head.addEventListener('click', function (e) {
            var th = e.target.closest('th.sortable');
            if (!th) return;
            var i = Array.prototype.indexOf.call(head.cells, th);
            if (i === sortCol) sortDir = -sortDir;
            else { sortCol = i; sortDir = 1; }
            render();
        });

        body.addEventListener('change', function (e) {
            var cb = e.target;
            if (cb.type !== 'checkbox') return;
            var key = cb.closest('tr').dataset.rowKey;
            if (cb.checked) selected.add(key); else selected.delete(key);
            render();
        });

        if (allBox) {
            allBox.addEventListener('change', function () {
                Array.prototype.forEach.call(body.rows, function (r) {
                    if (!r.dataset.rowKey) return;
                    if (allBox.checked) selected.add(r.dataset.rowKey);
                    else selected.delete(r.dataset.rowKey);
                });
                render();
            });
        }

        if (searchBox) {
            searchBox.addEventListener('input', function () {
                query = searchBox.value.trim();
                // See decision (2): a selection outside the filter is a Remove
                // aimed at something the user cannot see.
                selected.clear();
                page = 1;
                render();
            });
        }

        if (pager) {
            pager.addEventListener('click', function (e) {
                var b = e.target.closest('button');
                if (!b || b.disabled) return;
                var pages = Math.max(1, Math.ceil(matching().length / perPage));
                if (b.dataset.page) page = Number(b.dataset.page);
                else {
                    var label = b.getAttribute('aria-label');
                    if (label === 'First') page = 1;
                    else if (label === 'Previous') page = Math.max(1, page - 1);
                    else if (label === 'Next') page = Math.min(pages, page + 1);
                    else if (label === 'Last') page = pages;
                    else return;
                }
                render();
            });
            var sel = pager.querySelector('select');
            if (sel) {
                sel.addEventListener('change', function () {
                    perPage = Number(sel.value);
                    page = 1;
                    render();
                });
            }
        }

        render();
    }

    /* The mockups are static: every table they have is in the DOM at load, so
       initialising here is all they need. app.js's tables are not -- the view
       is empty until a screen's fetch resolves -- so this finds nothing there
       and the export is the only way in. Both entry points, because both
       cases are real. */
    window.initTable = initTable;
    document.querySelectorAll('.view table').forEach(function (t) { initTable(t); });
})();
