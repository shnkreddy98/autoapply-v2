/*
 * Apply-bench page runtime: widget behaviour, per-step validation, and the record of
 * what the page holds. Behaviour copies the live widgets the spec names (see each
 * site's "evidence"); the parts engines trip on are kept on purpose:
 *   - react-select: the menu renders outside .select__control, options exist only
 *     while open, async lists (location, school) stay empty until the user types, the
 *     shown value can be a slice of the option ("United States +1" -> "+1").
 *   - Workday prompt: a button that opens a body-level wd-popup; nothing to type into.
 *   - Workday multiselect: a search box; Enter shows matches, a click commits a chip.
 *   - Ashby yes/no: two plain buttons, the choice is only a class.
 *   - iCIMS search select: the real <select> is visually hidden behind a toggle.
 *   - Add / Save entry lists: an entry that is filled but never saved is not sent.
 * The value that counts is what the page would send, read from the DOM each tick and
 * posted to the bench server; the engine's own report is never trusted.
 */
(function () {
  const B = window.__BENCH;
  const FIELDS = B.fields;
  const byId = Object.fromEntries(FIELDS.map((f) => [f.id, f]));
  const REPEATS = Object.fromEntries((B.repeats || []).map((r) => [r.id, r]));
  // Every step is rendered, but only the current one stays in the document: Workday
  // mounts one step at a time, and a later step's hidden file input sitting in the DOM
  // took the resume upload on step 1. Detached steps keep their values for the record.
  const stepNodes = Array.from(document.querySelectorAll('.steps > .step'));
  const stepHost = document.querySelector('.steps');
  const wrap = (id) => {
    const f = byId[id];
    const root = f && stepNodes[f.step] ? stepNodes[f.step] : document;
    return root.querySelector('[data-bench-field="' + CSS.escape(id) + '"]');
  };
  // a field's own content: inside its shadow root on shadow steps (SmartRecruiters)
  const R = (w) => (w && w.shadowRoot) || w;
  const q = (w, sel) => R(w).querySelector(sel);
  const qa = (w, sel) => Array.from(R(w).querySelectorAll(sel));
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  let step = 0;
  const stepsSeen = [0];
  let submitted = false;
  const saved = {};  // repeat section id -> true once an entry was saved

  // ---------------------------------------------------------------- values

  function rawValue(f) {
    const w = wrap(f.id);
    if (!w) return null;
    switch (f.widget) {
      case 'text': case 'textarea':
        return q(w, 'input, textarea').value;
      case 'select': case 'search_select': {
        const el = q(w, 'select');
        return el.selectedIndex > 0 ? norm(el.options[el.selectedIndex].text) : '';
      }
      case 'radio': {
        const el = q(w, 'input:checked');
        return el ? el.value : '';
      }
      case 'checkbox':
        return q(w, 'input[type=checkbox]').checked;
      case 'checkbox_group':
        return qa(w, 'input:checked').map((el) => el.value);
      case 'react_select': case 'typeahead': case 'yesno': case 'wd_prompt': case 'buttons':
        return w.dataset.value || '';
      case 'wd_multiselect':
        return qa(w, '.chips li .chip-text').map((n) => norm(n.textContent));
      case 'multi_combo':
        return qa(w, '.mc-chips li .chip-text').map((n) => norm(n.textContent));
      case 'wd_date': {
        const parts = qa(w, '.seg').map((el) => el.value.trim());
        return parts.every((p) => !p) ? '' : parts.join('/');
      }
      case 'file':
        return Array.from(q(w, 'input[type=file]').files || []).map((x) => x.name);
    }
    return null;
  }

  function valueOf(f) {
    // an entry filled in but never saved is not part of the application
    if (f.section && !saved[f.section]) return null;
    return rawValue(f);
  }

  function isEmpty(v) {
    if (v === null || v === undefined || v === false) return true;
    if (Array.isArray(v)) return v.length === 0;
    return String(v).trim() === '';
  }

  function shown(f) {
    const w = wrap(f.id);
    return !!w && w.style.display !== 'none';
  }

  function collect() {
    const values = {};
    FIELDS.forEach((f) => { values[f.id] = { value: valueOf(f), shown: shown(f) }; });
    return { values, step, steps_seen: stepsSeen.slice(), step_title: B.steps[step], submitted, saved: Object.assign({}, saved) };
  }

  // ---------------------------------------------------------------- recording

  let lastSent = '';
  function post(path, body, beacon) {
    const data = JSON.stringify(body);
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon(B.base + path, new Blob([data], { type: 'application/json' }));
      return Promise.resolve();
    }
    return fetch(B.base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: data, keepalive: true })
      .catch(() => {});
  }
  function flush() {
    if (submitted) return Promise.resolve(null);
    const snap = collect();
    const data = JSON.stringify(snap);
    if (data === lastSent) return Promise.resolve(snap);
    lastSent = data;
    return post('/state', snap).then(() => snap);
  }
  window.__bench = { collect, flush };
  // after submit the form is gone: a later tick would record every field as missing
  setInterval(() => { if (!submitted) { applyShowIf(); flush(); } }, 400);
  window.addEventListener('pagehide', () => { if (!submitted) post('/state', collect(), true); });

  // ---------------------------------------------------------------- conditions

  function applyShowIf() {
    FIELDS.forEach((f) => {
      if (!f.show_if) return;
      const dep = byId[f.show_if.field];
      const v = dep ? rawValue(dep) : null;
      const want = f.show_if.equals;
      const on = typeof want === 'boolean' ? (v === want) : norm(v).toLowerCase() === norm(want).toLowerCase();
      const w = wrap(f.id);
      if (w) w.style.display = on ? '' : 'none';
    });
  }

  // ---------------------------------------------------------------- popups

  function closeAllPopups(except) {
    document.querySelectorAll('.bench-popup').forEach((p) => { if (p !== except) p._close(); });
  }
  document.addEventListener('mousedown', (e) => {
    const path = e.composedPath ? e.composedPath() : [e.target];
    const inside = path.some((n) => n.classList && (n.classList.contains('bench-popup') || n.classList.contains('field')));
    if (!inside) closeAllPopups();
  });

  function makePopup(owner, anchor, cls) {
    const pop = document.createElement('div');
    pop.className = 'bench-popup ' + cls;
    const r = anchor.getBoundingClientRect();
    pop.style.left = (r.left + window.scrollX) + 'px';
    pop.style.top = (r.bottom + window.scrollY + 2) + 'px';
    pop.style.minWidth = Math.max(r.width, 220) + 'px';
    pop._close = () => { pop.remove(); owner._pop = null; if (owner._onClose) owner._onClose(); };
    document.body.appendChild(pop);
    owner._pop = pop;
    return pop;
  }

  function optionList(items, onPick, attrs) {
    const ul = document.createElement('ul');
    ul.setAttribute('role', 'listbox');
    items.forEach((t) => {
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      Object.entries(attrs || {}).forEach(([k, v]) => li.setAttribute(k, v));
      li.tabIndex = -1;
      li.textContent = t;
      li.addEventListener('mousedown', (e) => e.preventDefault());
      li.addEventListener('click', () => onPick(t, li));
      ul.appendChild(li);
    });
    return ul;
  }

  // ---------------------------------------------------------------- widgets

  const INIT = {
    react_select(f, w) {
      const container = q(w, '[data-rs]');
      const input = q(w, 'input');
      const control = q(w, '.select__control');
      let menu = null, active = 0;
      const shownText = (t) => (f.display === 'last_word' ? t.split(' ').pop() : t);
      function close() {
        if (menu) { menu.remove(); menu = null; }
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-controls');
        input.removeAttribute('aria-activedescendant');
      }
      function pick(t) {
        const ph = control.querySelector('.select__placeholder, .select__single-value');
        ph.className = 'select__single-value';
        ph.textContent = shownText(t);
        w.dataset.value = t;
        input.value = '';
        changed(f.id);
        close();
      }
      function matches() {
        const s = input.value.toLowerCase().trim();
        if (f.async && s.length < 2) return null;  // "Type to search": nothing until typed
        const m = f.options.filter((t) => t.toLowerCase().includes(s));
        return s ? m : m.slice(0, 50);
      }
      function render() {
        const lb = menu.querySelector('[role=listbox]');
        lb.innerHTML = '';
        const m = matches();
        if (m === null || !m.length) {
          const empty = document.createElement('div');
          empty.className = 'select__menu-notice';
          empty.textContent = m === null ? 'Type to search' : 'No options';
          lb.appendChild(empty);
          return;
        }
        active = Math.min(active, m.length - 1);
        m.forEach((t, n) => {
          const o = document.createElement('div');
          o.className = 'select__option' + (n === active ? ' select__option--is-focused' : '');
          o.id = 'react-select-' + f.id + '-option-' + n;
          o.setAttribute('role', 'option');
          o.setAttribute('aria-selected', w.dataset.value === t ? 'true' : 'false');
          o.textContent = t;
          o.addEventListener('mousedown', (e) => e.preventDefault());
          o.addEventListener('click', () => pick(t));
          lb.appendChild(o);
        });
        input.setAttribute('aria-activedescendant', 'react-select-' + f.id + '-option-' + active);
      }
      function open() {
        if (menu) return;
        menu = document.createElement('div');
        menu.className = 'select__menu';
        const lb = document.createElement('div');
        lb.setAttribute('role', 'listbox');
        lb.id = 'react-select-' + f.id + '-listbox';
        menu.appendChild(lb);
        container.appendChild(menu);  // outside .select__control, like the real one
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-controls', lb.id);
        active = 0;
        render();
      }
      control.addEventListener('mousedown', (e) => { if (e.target !== input) e.preventDefault(); input.focus(); setTimeout(open, 10); });
      input.addEventListener('focus', () => setTimeout(open, 10));
      input.addEventListener('input', () => { active = 0; open(); render(); });
      input.addEventListener('blur', () => setTimeout(close, 150));
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { close(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault(); open(); active = Math.max(0, active + (e.key === 'ArrowDown' ? 1 : -1)); render(); return;
        }
        if (e.key === 'Enter' || e.key === 'Tab') {
          const m = menu ? matches() : null;
          if (m && m.length) { if (e.key === 'Enter') e.preventDefault(); pick(m[active] || m[0]); }
        }
      });
    },

    typeahead(f, w) {
      const input = q(w, 'input');
      const box = q(w, '.typeahead');
      const list = () => { const s = input.value.toLowerCase().trim(); return f.options.filter((t) => t.toLowerCase().includes(s)); };
      function pick(t) { input.value = t; w.dataset.value = t; changed(f.id); if (input._pop) input._pop._close(); }
      function open() {
        closeAllPopups(input._pop);
        const pop = input._pop || makePopup(input, box, 'ta-menu');
        pop.innerHTML = '';
        const lb = optionList(list(), (t) => pick(t));
        lb.id = f.id + '-listbox';
        pop.appendChild(lb);
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-controls', lb.id);
      }
      input._onClose = () => input.setAttribute('aria-expanded', 'false');
      input.addEventListener('input', () => { w.dataset.value = ''; open(); });
      input.addEventListener('focus', open);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { const m = list(); if (m.length) { e.preventDefault(); pick(m[0]); } }
        if (e.key === 'Escape' && input._pop) input._pop._close();
      });
      // text typed but never picked from the list is not an answer
      input.addEventListener('blur', () => setTimeout(() => {
        if (input.value !== w.dataset.value) input.value = w.dataset.value || '';
        if (input._pop) input._pop._close();
      }, 200));
      q(w, '.ta-toggle').addEventListener('click', () => { input.focus(); open(); });
    },

    yesno(f, w) {
      qa(w, 'button.yn').forEach((b) => b.addEventListener('click', () => {
        qa(w, 'button.yn').forEach((o) => {
          o.classList.toggle('_active', o === b);
          o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
        });
        q(w, 'input.yn-input').checked = b.dataset.v === 'Yes';
        w.dataset.value = b.dataset.v;
        changed(f.id);
      }));
    },

    buttons(f, w) {
      qa(w, 'button.pill').forEach((b) => b.addEventListener('click', () => {
        qa(w, 'button.pill').forEach((o) => o.setAttribute('aria-pressed', o === b ? 'true' : 'false'));
        w.dataset.value = b.dataset.v;
        changed(f.id);
      }));
    },

    search_select(f, w) {
      const sel = q(w, 'select');
      const toggle = q(w, '.ss-toggle');
      function sync() {
        toggle.textContent = sel.selectedIndex > 0 ? sel.options[sel.selectedIndex].text : '— Make a Selection —';
        changed(f.id);
      }
      sel.addEventListener('change', sync);  // select_option on the hidden native control
      toggle._onClose = () => toggle.setAttribute('aria-expanded', 'false');
      toggle.addEventListener('click', (e) => {
        e.preventDefault();
        if (toggle._pop) { toggle._pop._close(); return; }
        closeAllPopups();
        const pop = makePopup(toggle, toggle, 'ss-menu');
        const search = document.createElement('input');
        search.type = 'text';
        search.setAttribute('role', 'combobox');
        search.setAttribute('aria-label', '— Type to Search —');
        search.placeholder = '— Type to Search —';
        pop.appendChild(search);
        let lb = null;
        function render() {
          if (lb) lb.remove();
          const s = search.value.toLowerCase().trim();
          lb = optionList(['— Make a Selection —'].concat(f.options.filter((t) => t.toLowerCase().includes(s))), (t) => {
            sel.selectedIndex = Math.max(0, f.options.indexOf(t) + 1);
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            if (toggle._pop) toggle._pop._close();
            toggle.focus();
          });
          pop.appendChild(lb);
        }
        search.addEventListener('input', render);
        search.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter') {
            ev.preventDefault();
            const hit = f.options.find((t) => t.toLowerCase().includes(search.value.toLowerCase().trim()));
            if (hit) { sel.selectedIndex = f.options.indexOf(hit) + 1; sel.dispatchEvent(new Event('change', { bubbles: true })); toggle._pop._close(); }
          }
        });
        render();
        toggle.setAttribute('aria-expanded', 'true');
        search.focus();
      });
    },

    multi_combo(f, w) {
      const input = q(w, 'input');
      const chips = q(w, '.mc-chips');
      const box = q(w, '.mc');
      const have = () => qa(w, '.mc-chips .chip-text').map((n) => n.textContent);
      function add(t) {
        if (have().includes(t)) return;
        const li = document.createElement('li');
        li.innerHTML = '<span class="chip-text"></span><button type="button" class="chip-x">✕</button>';
        li.querySelector('.chip-text').textContent = t;
        li.querySelector('.chip-x').setAttribute('aria-label', 'Delete ' + t);
        li.querySelector('.chip-x').addEventListener('click', () => { li.remove(); changed(f.id); });
        chips.appendChild(li);
        input.value = '';
        changed(f.id);
        open();  // the list stays open for the next pick
      }
      function open() {
        closeAllPopups(input._pop);
        const pop = input._pop || makePopup(input, box, 'mc-menu');
        pop.innerHTML = '';
        const s = input.value.toLowerCase().trim();
        const lb = optionList(f.options.filter((t) => !have().includes(t) && t.toLowerCase().includes(s)), (t) => add(t));
        lb.id = f.id + '-listbox';
        pop.appendChild(lb);
        input.setAttribute('aria-expanded', 'true');
        input.setAttribute('aria-controls', lb.id);
      }
      input._onClose = () => input.setAttribute('aria-expanded', 'false');
      input.addEventListener('focus', open);
      input.addEventListener('click', open);
      input.addEventListener('input', open);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const s = input.value.toLowerCase().trim();
          const hit = s && f.options.find((t) => t.toLowerCase().includes(s) && !have().includes(t));
          if (hit) add(hit);
        }
        if (e.key === 'Escape' && input._pop) input._pop._close();
      });
      q(w, '.mc-clear').addEventListener('click', () => { chips.innerHTML = ''; changed(f.id); });
    },

    wd_prompt(f, w) {
      const btn = q(w, 'button.wd-prompt');
      const req = f.required ? ' Required' : '';
      function commit(t) {
        btn.textContent = t;
        btn.setAttribute('aria-label', f.label + ' ' + t + req);
        w.dataset.value = t;
        changed(f.id);
        if (btn._pop) btn._pop._close();
        btn.focus();
      }
      btn._onClose = () => btn.setAttribute('aria-expanded', 'false');
      btn.addEventListener('click', () => {
        if (btn._pop) { btn._pop._close(); return; }
        closeAllPopups();
        const pop = makePopup(btn, btn, 'wd-popup');
        pop.setAttribute('data-automation-widget', 'wd-popup');
        pop.setAttribute('data-associated-widget', btn.id);
        const ul = optionList(f.options, (t) => commit(t), { 'data-automation-id': 'promptOption' });
        ul.id = btn.id + '-listbox';
        pop.appendChild(ul);
        btn.setAttribute('aria-expanded', 'true');
        btn.setAttribute('aria-controls', ul.id);
      });
    },

    wd_multiselect(f, w) {
      const box = q(w, '.wd-multi');
      const input = q(w, 'input');
      const chips = q(w, '.chips');
      const leaves = [];
      Object.entries(f.tree).forEach(([k, v]) => { if (Array.isArray(v)) v.forEach((x) => leaves.push(x)); else leaves.push(k); });
      function commit(t) {
        chips.innerHTML = '';
        const li = document.createElement('li');
        li.innerHTML = '<span class="chip-text"></span><button type="button" class="chip-x" aria-label="Remove">&times;</button>';
        li.querySelector('.chip-text').textContent = t;
        li.querySelector('.chip-x').addEventListener('click', () => li.remove());
        chips.appendChild(li);
        input.value = '';
        changed(f.id);
        if (input._pop) input._pop._close();
      }
      function show(items, isCategoryLevel) {
        closeAllPopups(input._pop);
        const pop = input._pop || makePopup(input, box, 'wd-popup');
        pop.setAttribute('data-automation-widget', 'wd-popup');
        pop.setAttribute('data-associated-widget', box.id);
        pop.innerHTML = '';
        const ul = document.createElement('ul');
        ul.setAttribute('role', 'listbox');
        if (!items.length) {
          const li = document.createElement('li');
          li.className = 'no-items';
          li.textContent = 'No Items.';
          ul.appendChild(li);
        }
        items.forEach((t) => {
          const kids = isCategoryLevel && Array.isArray(f.tree[t]) ? f.tree[t] : null;
          const li = document.createElement('li');
          li.setAttribute('role', 'option');
          li.setAttribute('data-automation-id', 'promptOption');
          li.tabIndex = -1;
          li.textContent = t;
          if (kids) { li.setAttribute('aria-haspopup', 'true'); li.classList.add('category'); }
          li.addEventListener('mousedown', (e) => e.preventDefault());
          li.addEventListener('click', () => { if (kids) show(kids, false); else commit(t); });
          ul.appendChild(li);
        });
        pop.appendChild(ul);
      }
      input.addEventListener('click', () => { if (!input._pop) show(Object.keys(f.tree), true); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const s = input.value.toLowerCase().trim();
          show(s ? leaves.filter((t) => t.toLowerCase().includes(s)) : Object.keys(f.tree), !s);
        }
        if (e.key === 'Escape' && input._pop) input._pop._close();
      });
    },

    wd_date(f, w) {
      // digits only, move on when a segment is full
      const segs = qa(w, '.seg');
      segs.forEach((s, n) => s.addEventListener('input', () => {
        s.value = s.value.replace(/\D/g, '').slice(0, s.maxLength);
        changed(f.id);
        if (s.value.length === s.maxLength && segs[n + 1]) segs[n + 1].focus();
      }));
    },

    checkbox_group(f, w) {
      if (!f.single) return;
      // a checkbox group that behaves as a single choice (Workday CC-305, JazzHR)
      const boxes = qa(w, 'input[type=checkbox]');
      boxes.forEach((b) => b.addEventListener('change', () => { if (b.checked) boxes.forEach((o) => { if (o !== b) o.checked = false; }); }));
    },

    file(f, w) {
      const input = q(w, 'input[type=file]');
      const trigger = q(w, '.file-trigger');
      const name = q(w, '.file-name');
      if (trigger) trigger.addEventListener('click', () => input.click());
      input.addEventListener('change', () => {
        const files = Array.from(input.files || []);
        name.textContent = files.map((x) => x.name).join(', ');
        if (files.length) {
          const rm = document.createElement('button');
          rm.type = 'button'; rm.className = 'file-remove'; rm.textContent = 'Remove file';
          rm.addEventListener('click', () => { input.value = ''; name.textContent = ''; changed(f.id); });
          name.appendChild(rm);
        }
        if (f.copies_to && files.length) {
          // Ashby's "Autofill from resume" attaches the same file to the Resume field
          const target = q(wrap(f.copies_to), 'input[type=file]');
          const dt = new DataTransfer();
          files.forEach((x) => dt.items.add(x));
          target.files = dt.files;
          target.dispatchEvent(new Event('change', { bubbles: true }));
        }
        changed(f.id);
      });
    },
  };

  FIELDS.forEach((f) => {
    const w = wrap(f.id);
    if (!w) return;
    if (INIT[f.widget]) INIT[f.widget](f, w);
    qa(w, 'input, textarea, select').forEach((el) => {
      el.addEventListener('input', () => changed(f.id));
      el.addEventListener('change', () => changed(f.id));
    });
  });

  // ---------------------------------------------------------------- Add / Save entries

  function sectionNode(id) {
    const r = REPEATS[id];
    return stepNodes[r.step].querySelector('[data-section="' + id + '"]');
  }
  Object.keys(REPEATS).forEach((id) => {
    const node = sectionNode(id);
    const r = REPEATS[id];
    const form = node.querySelector('.repeat-form');
    const add = node.querySelector('.btn-add');
    const list = node.querySelector('.repeat-list');
    const mine = FIELDS.filter((f) => f.section === id);
    add.addEventListener('click', () => { form.style.display = ''; add.style.display = 'none'; });
    node.querySelector('.btn-cancel').addEventListener('click', () => { form.style.display = 'none'; add.style.display = ''; });
    node.querySelector('.btn-save').addEventListener('click', () => {
      const bad = mine.filter((f) => f.required && shown(f) && isEmpty(rawValue(f)));
      mine.forEach((f) => clearError(f.id));
      bad.forEach((f) => setError(f.id));
      if (bad.length) return;
      saved[id] = true;
      form.style.display = 'none';
      add.style.display = '';
      const summaryField = byId[r.summary];
      const title = summaryField ? rawValue(summaryField) : '';
      list.innerHTML = '';
      const row = document.createElement('div');
      row.className = 'repeat-entry';
      row.innerHTML = '<span class="entry-title"></span><button type="button" class="entry-edit"></button><button type="button" class="entry-delete"></button>';
      row.querySelector('.entry-title').textContent = title;
      row.querySelector('.entry-edit').textContent = 'Edit ' + r.noun + ' ' + title;
      row.querySelector('.entry-delete').textContent = 'Delete ' + r.noun + ' ' + title;
      row.querySelector('.entry-edit').addEventListener('click', () => { form.style.display = ''; add.style.display = 'none'; });
      row.querySelector('.entry-delete').addEventListener('click', () => { saved[id] = false; row.remove(); flush(); });
      list.appendChild(row);
      node.querySelector('.repeat-error').textContent = '';
      flush();
    });
  });

  // ---------------------------------------------------------------- overlay

  function showOverlay(o) {
    // a modal over the page (cookie banner, notice): clicks underneath are blocked
    // until one of its buttons is pressed, as on the live sites
    const wrapEl = document.createElement('div');
    wrapEl.className = 'overlay-wrap ' + (o.wrap_class || '');
    const box = document.createElement('div');
    box.className = 'overlay ' + (o.class || '');
    box.setAttribute('role', o.role || 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', o.title || 'Notice');
    box.innerHTML = '<h2></h2><p></p><div class="overlay-buttons"></div>';
    box.querySelector('h2').textContent = o.title || '';
    box.querySelector('p').textContent = o.text || '';
    (o.buttons || ['OK']).forEach((t) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = t;
      b.addEventListener('click', () => { if (!o.keep || !o.keep.includes(t)) wrapEl.remove(); });
      box.querySelector('.overlay-buttons').appendChild(b);
    });
    wrapEl.appendChild(box);
    document.body.appendChild(wrapEl);
  }

  // ---------------------------------------------------------------- validation

  const MESSAGES = {
    greenhouse: 'This field is required.', ashby: 'This field is required', lever: 'This field is required',
    workday: 'The field {label} is required and must have a value.', oracle: 'The {label} field is required.',
    icims: 'This field is required.', ukg: '{label} is required', adp: '{label} is required.',
  };

  function control(w) {
    return q(w, 'input:not([type=hidden]):not(.visually-hidden), textarea, select:not(.ss-native), button.wd-prompt, fieldset, .yesno, .pills, .choices, .ss-toggle') || w;
  }
  function setError(id) {
    const w = wrap(id), f = byId[id];
    const msg = (MESSAGES[B.style] || 'Required').replace('{label}', f.label);
    const box = q(w, '.field-error');
    box.textContent = msg;
    box.classList.add('error');
    control(w).setAttribute('aria-invalid', 'true');
    w.classList.add('has-error');
  }
  function clearError(id) {
    const w = wrap(id);
    if (!w || !w.classList.contains('has-error')) return;
    const box = q(w, '.field-error');
    box.textContent = '';
    box.classList.remove('error');
    control(w).removeAttribute('aria-invalid');
    w.classList.remove('has-error');
  }
  function changed(id) {
    // every widget commit lands here: reveal dependent questions at once, like the
    // live form (Greenhouse shows race the moment Hispanic/Latino = No is picked)
    applyShowIf();
    clearError(id);
  }
  function validate(n) {
    applyShowIf();
    const bad = FIELDS.filter((f) => f.step === n && !f.section && f.required && shown(f) && isEmpty(valueOf(f)));
    const unsaved = Object.values(REPEATS).filter((r) => r.step === n && r.required && !saved[r.id]);
    FIELDS.filter((f) => f.step === n && !f.section).forEach((f) => clearError(f.id));
    bad.forEach((f) => setError(f.id));
    unsaved.forEach((r) => { sectionNode(r.id).querySelector('.repeat-error').textContent = r.heading + ' is required'; });
    const labels = bad.map((f) => f.label).concat(unsaved.map((r) => r.heading));
    const banner = document.querySelector('.error-banner');
    if (banner) {
      if (labels.length && B.style === 'workday') {
        banner.textContent = 'Errors Found: ' + labels.length + (labels.length === 1 ? ' error' : ' errors') + ' — ' + labels.join('; ');
        banner.style.display = '';
      } else if (labels.length && B.style === 'ashby') {
        banner.textContent = 'Your form needs corrections: ' + labels.map((l) => 'Missing entry for required field: ' + l).join(' ');
        banner.style.display = '';
      } else if (labels.length && B.style !== 'greenhouse' && B.style !== 'lever') {
        banner.textContent = 'Please complete the required fields: ' + labels.join('; ');
        banner.style.display = '';
      } else {
        banner.style.display = 'none';
      }
    }
    if (bad.length) {
      const first = wrap(bad[0].id);
      if (first.scrollIntoView) first.scrollIntoView({ block: 'center' });
    }
    return labels.length === 0;
  }

  // ---------------------------------------------------------------- navigation

  function showStep(n) {
    closeAllPopups();
    step = n;
    if (!stepsSeen.includes(n)) stepsSeen.push(n);
    stepNodes.forEach((s, k) => s.classList.toggle('on', k === n));
    stepHost.replaceChildren(stepNodes[n]);
    document.querySelectorAll('.progress li').forEach((li) => {
      const k = Number(li.dataset.step);
      li.classList.toggle('active', k === n);
      li.classList.toggle('done', k < n);
      if (k === n) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
    });
    const count = document.querySelector('.step-count');
    if (count) count.textContent = 'current step ' + (n + 1) + ' of ' + B.steps.length;
    window.scrollTo(0, 0);
    flush();
  }

  function onActivate(el, fn) {
    el.addEventListener('click', fn);
    // custom-element buttons (UKG) answer Enter and Space like a button would
    if (el.tagName.includes('-')) el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); } });
  }
  stepNodes.forEach((node) => {
    node.querySelectorAll('.bench-next').forEach((b) => onActivate(b, () => {
      if (!validate(step)) { flush(); return; }
      setTimeout(() => showStep(step + 1), 250);  // Workday saves before it moves on
    }));
    node.querySelectorAll('.btn-back').forEach((b) => onActivate(b, () => showStep(Math.max(0, step - 1))));
    node.querySelectorAll('.bench-submit').forEach((b) => onActivate(b, () => {
      // every step, not just this one: a single-page form is one step, and Workday's
      // Review step has no fields of its own
      for (let n = 0; n < B.steps.length; n++) {
        if (!validate(n)) {
          if (n !== step) { showStep(n); validate(n); }
          flush();
          return;
        }
      }
      const snap = collect();
      snap.submitted = submitted = true;
      post('/submit', snap).then(() => {
        document.getElementById('application-form').outerHTML =
          '<div class="confirmation" role="status"><h2>' + B.confirmation.split('. ')[0] + '</h2><p>' + B.confirmation + '</p></div>';
      });
    }));
  });

  stepHost.replaceChildren(stepNodes[0]);
  applyShowIf();
  if (B.overlay) setTimeout(() => showOverlay(B.overlay), B.overlay.delay_ms || 600);
  flush();
})();
