() => {
  /*
   * Read-only field extractor, run in the page via Playwright MCP `browser_evaluate`.
   * Returns a JSON string: one descriptor per form control. Writes nothing to the
   * DOM — classification happens in Python (det_autofill.classify), and actions go
   * back through MCP with the `selector` each descriptor carries.
   *
   * Label strategies and fillability signals are ported from Autofill's
   * job-autofill.js (bestLabel / groupLabel / collectContext / isVisible), which
   * documents why each one exists.
   */
  const CANDIDATES = [
    'input', 'select', 'textarea',
    '[role="combobox"]', '[role="listbox"]', '[aria-haspopup="listbox"]',
    '[role="radio"]', '[role="checkbox"]', '[role="switch"]',
  ].join(', ');
  const SKIP_INPUT_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);

  const textOf = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&'));

  function labelledBy(el) {
    const ids = (el.getAttribute('aria-labelledby') || '').trim();
    if (!ids) return '';
    return ids.split(/\s+/).map((id) => textOf(document.getElementById(id))).filter(Boolean).join(' ');
  }

  function bestLabel(el) {
    if (el.labels && el.labels.length) {
      const t = textOf(el.labels[0]);
      if (t) return t;
    }
    const id = el.getAttribute('id');
    if (id) {
      const lf = document.querySelector(`label[for="${cssEscape(id)}"]`);
      if (lf && textOf(lf)) return textOf(lf);
    }
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    const al = labelledBy(el);
    if (al) return al;
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const wrapper = el.closest(
      '[data-automation-id^="formField-"], [data-field-entry-id], [data-testid="FieldEntry"], ' +
      '.application-question, .field, .form-group, .application-field, [class*="field" i]'
    );
    if (wrapper) {
      const lbl = wrapper.querySelector('label, legend, [class*="label" i]');
      if (lbl && !lbl.contains(el) && textOf(lbl)) return textOf(lbl);
    }
    // Lever: <li class="application-question"><div class="application-label">…</div>
    // <div class="application-field"><select>…</select></div></li>
    const question = el.closest('.application-question, [class*="question" i]');
    if (question) {
      const lbl = Array.from(question.querySelectorAll('[class*="label" i], label')).find((n) => !n.contains(el) && textOf(n));
      if (lbl) return textOf(lbl);
    }
    for (let prev = el.previousElementSibling; prev; prev = prev.previousElementSibling) {
      if (/^(LABEL|LEGEND|SPAN|DIV|P)$/.test(prev.tagName)) {
        const t = textOf(prev);
        if (t && t.length <= 120) return t;
      }
    }
    return '';
  }

  function groupScope(el) {
    // .application-question: Lever keeps the question text in a sibling
    // div.application-label, outside the <ul> of choices
    return el.closest(
      'fieldset, [role="radiogroup"], [role="group"], [data-automation-id^="formField-"], ' +
      '[data-field-entry-id], .field, .form-group, .application-question, [class*="question" i]'
    );
  }

  function groupLabel(el) {
    const scope = groupScope(el);
    if (!scope) return '';
    // a lone checkbox inside its own <label> is its own question: Lever puts both
    // consent boxes in one question block, and borrowing the neighbour's text swapped
    // the required "retain my data" box with the optional marketing one
    if (el.closest('label')) {
      const peers = el.name
        ? scope.querySelectorAll(`input[name="${cssEscape(el.name)}"]:not([type="hidden"])`)
        : scope.querySelectorAll('input[type="checkbox"], input[type="radio"]');
      if (peers.length <= 1) return '';
    }
    const legend = scope.querySelector('legend');
    if (legend && textOf(legend)) return textOf(legend);
    const aria = (scope.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const lab = labelledBy(scope);
    if (lab) return lab;
    // a label wrapping another control names that control, not this group
    const lbl = Array.from(scope.querySelectorAll('label, [class*="label" i]'))
      .find((n) => !n.contains(el) && !n.querySelector('input, select, textarea') && textOf(n));
    if (lbl) return textOf(lbl);
    return '';
  }

  /* Nearest enclosing section title: a fieldset legend, a labelled group, or the
   * closest heading above. Used to refuse identity fields inside "Reference 1" or
   * "Emergency contact" blocks. */
  function sectionOf(el) {
    for (let node = el, depth = 0; node && node !== document.body && depth < 8; node = node.parentElement, depth++) {
      if (node.tagName === 'FIELDSET') {
        const lg = node.querySelector(':scope > legend');
        if (lg && textOf(lg)) return textOf(lg).slice(0, 80);
      }
      let sib = node.previousElementSibling;
      for (let n = 0; sib && n < 12; sib = sib.previousElementSibling, n++) {
        if (/^H[1-6]$/.test(sib.tagName) || sib.getAttribute('role') === 'heading') {
          return textOf(sib).slice(0, 80);
        }
      }
    }
    return '';
  }

  function isVisible(el) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2;
  }

  function widgetOf(el, tag, type) {
    const role = el.getAttribute('role');
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textarea';
    if (type === 'radio' || role === 'radio') return 'radio';
    if (type === 'checkbox' || role === 'checkbox' || role === 'switch') return 'checkbox';
    if (type === 'file') return 'file';
    if (role === 'combobox' || role === 'listbox' || el.getAttribute('aria-haspopup') === 'listbox') return 'combobox';
    if (type === 'date') return 'date';
    return 'text';
  }

  function unique(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }

  /* A CSS selector that matches exactly this element, so MCP actions can target it
   * without snapshot refs (which die after every mutation). */
  function selectorFor(el) {
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute('id');
    if (id && unique(`#${cssEscape(id)}`)) return `#${cssEscape(id)}`;
    const name = el.getAttribute('name');
    const type = el.getAttribute('type');
    if (name) {
      const byName = `${tag}[name="${cssEscape(name)}"]`;
      if (unique(byName)) return byName;
      if (type && el.value) {
        const byValue = `${byName}[value="${cssEscape(el.value)}"]`;
        if (unique(byValue)) return byValue;
      }
    }
    const aid = el.getAttribute('data-automation-id');
    if (aid && unique(`[data-automation-id="${cssEscape(aid)}"]`)) return `[data-automation-id="${cssEscape(aid)}"]`;
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      const nid = node.getAttribute('id');
      if (node !== el && nid && unique(`#${cssEscape(nid)}`)) {
        parts.unshift(`#${cssEscape(nid)}`);
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      if (unique(parts.join(' > '))) break;
    }
    return parts.join(' > ');
  }

  function groupKeyFor(el) {
    const name = el.getAttribute('name');
    if (name) return `name:${name}`;
    const scope = groupScope(el);
    return scope ? `scope:${selectorFor(scope)}` : `el:${selectorFor(el)}`;
  }

  /* What a custom dropdown currently shows (react-select single value, Workday
   * prompt button caption), so an already-answered combobox reads as filled. */
  function displayText(el, widget) {
    if (widget !== 'combobox') return '';
    // first ancestor holding a chosen value, never one shared with another dropdown
    // (live react-select nests the input in its own "select__input-container")
    for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
      if (Array.from(n.querySelectorAll('[role="combobox"], select')).some((c) => c !== el)) break;
      const single = n.querySelector('[class*="singleValue" i], [class*="single-value" i], [data-automation-id="selectedItem"]');
      if (single && textOf(single)) return textOf(single);
    }
    // buttons and div-based comboboxes show the pick as their own text
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return textOf(el);
    return el.value || '';
  }

  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll(CANDIDATES)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : '')).toLowerCase();
    if (tag === 'input' && SKIP_INPUT_TYPES.has(type)) continue;
    // react-select and friends nest a role=combobox input inside a role=combobox
    // wrapper; describe the outermost once
    if (el.parentElement && el.parentElement.closest('[role="combobox"]') && el.getAttribute('role') === 'combobox') continue;

    const widget = widgetOf(el, tag, type);
    const isChoice = widget === 'radio' || widget === 'checkbox';
    const label = bestLabel(el);
    const gl = isChoice ? groupLabel(el) : '';
    const d = {
      i: out.length,
      selector: selectorFor(el),
      tag,
      type,
      widget,
      role: el.getAttribute('role') || '',
      name: el.getAttribute('name') || '',
      id: el.getAttribute('id') || '',
      autocomplete: (el.getAttribute('autocomplete') || '').trim().toLowerCase(),
      automation_id: el.getAttribute('data-automation-id') || '',
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      label: gl || label,
      group_label: gl,
      option_label: isChoice ? label : '',
      section: sectionOf(el),
      required: !!(el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$|\(required\)/i.test(gl || label)),
      value: widget === 'select' ? (el.selectedIndex >= 0 && el.options[el.selectedIndex] ? textOf(el.options[el.selectedIndex]) : '') : (el.value || ''),
      display: displayText(el, widget),
      checked: isChoice ? !!(el.checked || el.getAttribute('aria-checked') === 'true') : null,
      options: widget === 'select' ? Array.from(el.options).map((o) => ({ value: o.value, text: textOf(o) })) : [],
      group_key: isChoice ? groupKeyFor(el) : '',
      controls: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '',
      expanded: el.getAttribute('aria-expanded') === 'true',
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      readonly: !!el.readOnly,
      aria_hidden: !!el.closest('[aria-hidden="true"]'),
      visible: isVisible(el),
      maxlength: parseInt(el.getAttribute('maxlength') || '0', 10) || 0,
      has_files: widget === 'file' ? !!(el.files && el.files.length) : false,
      // Workday multiselects keep committed picks as chips beside an empty search box
      chips: (() => {
        const field = el.closest('[data-automation-id^="formField-"], [data-automation-id="multiSelectContainer"]');
        return field ? Array.from(field.querySelectorAll('[data-automation-id="selectedItemList"] li')).map(textOf).filter(Boolean) : [];
      })(),
    };
    out.push(d);
  }
  return JSON.stringify({ url: location.href, fields: out });
}
