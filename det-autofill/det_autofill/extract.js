() => {
/*FRAMES*/
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
    // Oracle Candidate Experience: <span class="apply-flow-input-checkbox__button">
    // with a knockout click binding, no input and no role
    '[class*="checkbox__button" i]',
  ].join(', ');
  const SKIP_INPUT_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image']);

  // icons carry fallback text ("SVGs not supported by this browser." in Workable's
  // <svg><desc>), which became the resume field's label
  const textOf = (el) => {
    if (!el) return '';
    const raw = el.querySelector && el.querySelector('svg') ? stripSvg(el) : (el.innerText || el.textContent || '');
    return raw.replace(/\s+/g, ' ').trim();
  };
  function stripSvg(el) {
    const copy = el.cloneNode(true);
    copy.querySelectorAll('svg').forEach((n) => n.remove());
    return copy.textContent || '';
  }
  const UPLOAD_HINT = /^(\+\s*)?(upload|drop|drag|select|choose|attach|browse)\b/i;
  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/[^\w-]/g, '\\$&'));

  function labelledBy(el) {
    const ids = (el.getAttribute('aria-labelledby') || '').trim();
    if (!ids) return '';
    return ids.split(/\s+/).map((id) => textOf(el.ownerDocument.getElementById(id))).filter(Boolean).join(' ');
  }

  function rendered(n) {
    const st = getComputedStyle(n);
    return st.display !== 'none' && st.visibility !== 'hidden';
  }

  /* The text just before the control, or before one of its wrappers, climbing only
   * while the wrapper holds nothing but this question's own controls. Breezy puts an
   * <h3> before each input and a hidden "A full name is required" after it (which the
   * next field used to take as its label); Rippling puts the question in a sibling
   * <div><p> before the field's wrapper, three levels up. */
  const CONTROLS = 'input:not([type="hidden"]), select, textarea, [role="radio"], [role="checkbox"], [role="combobox"]';
  function textBefore(start, own) {
    const mine = (n) => own.some((o) => o === n || o.contains(n) || n.contains(o));
    for (let node = start, depth = 0; node && node !== start.ownerDocument.body && depth < 6; node = node.parentElement, depth++) {
      if (Array.from(node.querySelectorAll(CONTROLS)).some((c) => !mine(c))) break;
      for (let prev = node.previousElementSibling; prev; prev = prev.previousElementSibling) {
        if (!rendered(prev) || /error|invalid|hint|screen-reader/i.test(`${prev.className} ${prev.getAttribute('data-testid') || ''}`)) continue;
        if (prev.matches(CONTROLS) || prev.querySelector(CONTROLS)) return '';
        if (/^(LABEL|LEGEND|SPAN|DIV|P|H[1-6])$/.test(prev.tagName)) {
          const t = textOf(prev);
          if (t && t.length <= 300 && !UPLOAD_HINT.test(t)) return t;
        }
      }
    }
    return '';
  }

  function bestLabel(el) {
    if (el.labels && el.labels.length) {
      const own = Array.from(el.labels).map((l) => labelledBy(l) || textOf(l)).filter(Boolean);
      const named = own.find((t) => !UPLOAD_HINT.test(t));
      if (named) return named;
      // a drop zone's labels only say "Upload a file": the field's name sits before it
      if (own.length) return (el.type === 'file' && textBefore(el, [el])) || own[0];
    }
    const id = el.getAttribute('id');
    if (id) {
      const lf = el.ownerDocument.querySelector(`label[for="${cssEscape(id)}"]`);
      if (lf && textOf(lf)) return textOf(lf);
    }
    const wrap = el.closest('label');
    // Rippling's upload <label aria-labelledby="… field-4-label"> reads "Drop or
    // select (.pdf)"; the field name is what it points at
    if (wrap && labelledBy(wrap)) return labelledBy(wrap);
    if (wrap && textOf(wrap)) return textOf(wrap);
    const al = labelledBy(el);
    if (al) return al;
    if (/checkbox__button/i.test(String(el.className)) && el.parentElement) {
      const lbl = el.parentElement.querySelector('[class*="checkbox__label" i]');
      if (lbl && textOf(lbl)) return textOf(lbl);
    }
    const aria = (el.getAttribute('aria-label') || '').trim();
    // "file-input" (BambooHR) is an id, not a name: keep looking
    if (aria && !/^[a-z0-9]+([_-][a-z0-9]+)+$/i.test(aria)) return aria;
    // a div with role=radio names its own option (Rippling: <div role="radio"><p>Yes</p>)
    if (el.tagName !== 'INPUT' && /^(radio|checkbox)$/.test(el.getAttribute('role') || '')) {
      const own = textOf(el);
      if (own && own.length <= 120) return own;
    }
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
    // Breezy: <li class="question"><div class="dropdown"><h3>…</h3><div class="dropdown-container">
    const question = el.closest('.application-question, [class*="question" i]');
    if (question) {
      const lbl = Array.from(question.querySelectorAll('[class*="label" i], label, h2, h3, h4, h5')).find((n) => !n.contains(el) && rendered(n) && textOf(n));
      if (lbl) return textOf(lbl);
    }
    return textBefore(el, [el]);
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
    if (!scope) return headingBefore(el);
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
    // the group's own <label for="radiogroup-id"> (Eightfold)
    if (scope.id) {
      const forScope = scope.ownerDocument.querySelector(`label[for="${cssEscape(scope.id)}"]`);
      if (forScope && textOf(forScope)) return textOf(forScope);
    }
    // a label wrapping or pointing at another control names that control, not this group
    const lbl = Array.from(scope.querySelectorAll('label, [class*="label" i]'))
      .find((n) => !n.contains(el) && !n.querySelector('input, select, textarea') && textOf(n)
        && !(n.htmlFor && scope.querySelector(`[id="${cssEscape(n.htmlFor)}"]`)));
    if (lbl) return textOf(lbl);
    return headingBefore(el) || textBefore(scope, Array.from(scope.querySelectorAll(CONTROLS)));
  }

  /* Breezy EEO: <div class="multiplechoice"><h3>Race or Ethnicity</h3><ul> of radios,
   * with no fieldset or labelled group: the heading inside the smallest ancestor
   * holding every radio of the same name. */
  function headingBefore(el) {
    if (!el.name) return '';
    const peers = el.ownerDocument.querySelectorAll(`input[name="${cssEscape(el.name)}"]`);
    let box = el.parentElement;
    const body = el.ownerDocument.body;
    while (box && box !== body && Array.from(peers).some((p) => !box.contains(p))) box = box.parentElement;
    if (!box || box === body || peers.length < 2) return '';
    // the heading can sit inside that ancestor or just before it, as long as the
    // wider box holds no other question's controls
    for (let n = 0; box && n < 2; box = box.parentElement, n++) {
      const others = Array.from(box.querySelectorAll('input, select, textarea')).filter((i) => i.name !== el.name && i.type !== 'hidden');
      if (others.length) break;
      const h = Array.from(box.querySelectorAll('h2, h3, h4, h5, legend, p')).find((x) => rendered(x) && !x.querySelector('input') && textOf(x) && textOf(x).length <= 160);
      if (h) return textOf(h);
    }
    return '';
  }

  /* Nearest enclosing section title: a fieldset legend, a labelled group, or the
   * closest heading above. Used to refuse identity fields inside "Reference 1" or
   * "Emergency contact" blocks. */
  function sectionOf(el) {
    for (let node = el, depth = 0; node && node !== el.ownerDocument.body && depth < 8; node = node.parentElement, depth++) {
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
    if (tag !== 'input' && /checkbox__button/i.test(String(el.className))) return 'checkbox';
    if (type === 'file') return 'file';
    if (role === 'combobox' || role === 'listbox' || el.getAttribute('aria-haspopup') === 'listbox') return 'combobox';
    if (type === 'date') return 'date';
    return 'text';
  }

  /* `unique` is rebound per element by selectorFor: uniqueness only holds inside the
   * element's own document (a frame's selector is prefixed with the hop into it). */
  let unique = () => false;

  /* A CSS selector that matches exactly this element, so MCP actions can target it
   * without snapshot refs (which die after every mutation). */
  function selectorFor(el) {
    const doc = el.ownerDocument;
    unique = (sel) => { try { return doc.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
    return framePrefix(doc) + localSelector(el, doc);
  }

  function localSelector(el, doc) {
    const tag = el.tagName.toLowerCase();
    const id = el.getAttribute('id');
    const name = el.getAttribute('name');
    // counter ids ("FabricTextField-345") are renumbered when the form re-renders —
    // BambooHR's were, between planning and typing, so the address went to another
    // box; a unique name survives that
    const generated = id && /(^:r|[-_:]\d+$|\d{3,})/.test(id);
    if (generated && name && unique(`${tag}[name="${cssEscape(name)}"]`)) return `${tag}[name="${cssEscape(name)}"]`;
    if (id && unique(`#${cssEscape(id)}`)) return `#${cssEscape(id)}`;
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
    for (let node = el; node && node.nodeType === 1 && node !== doc.documentElement; node = node.parentElement) {
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
      const single = n.querySelector('[class*="singleValue" i], [class*="single-value" i], [data-automation-id="selectedItem"], .ant-select-selection-item');
      if (single && textOf(single)) return textOf(single);
    }
    // buttons and div-based comboboxes show the pick as their own text
    if (!/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return textOf(el);
    return el.value || '';
  }

  const seen = new Set();
  const out = [];
  for (const el of deepAll(CANDIDATES)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : '')).toLowerCase();
    if (tag === 'input' && SKIP_INPUT_TYPES.has(type)) continue;
    // react-select and friends nest a role=combobox input inside a role=combobox
    // wrapper; describe the outermost once
    if (el.parentElement && el.parentElement.closest('[role="combobox"]') && el.getAttribute('role') === 'combobox') continue;

    // Jobvite draws <i role="radio" aria-selected> inside the <label> of a real radio;
    // describing both made two groups for one question. The native input wins when a
    // user can reach it (itself or its label); Rippling's is unreachable, so its
    // role=radio div stays.
    if (tag !== 'input' && /^(radio|checkbox)$/.test(el.getAttribute('role') || '')) {
      const host = el.closest('label') || el;
      const native = host.querySelector('input[type="radio"], input[type="checkbox"]');
      if (native && native !== el && (isVisible(native) || (host.tagName === 'LABEL' && isVisible(host)))) continue;
    }
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
      required: !!(el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$|\(required\)/i.test(gl || label)
        || (el.parentElement && !!el.parentElement.querySelector('[class*="label--required" i]'))),
      value: widget === 'select' ? (el.selectedIndex >= 0 && el.options[el.selectedIndex] ? textOf(el.options[el.selectedIndex]) : '') : (el.value || ''),
      display: displayText(el, widget),
      checked: isChoice ? !!(el.checked || el.getAttribute('aria-checked') === 'true' || /--checked\b/i.test(String(el.className))) : null,
      options: widget === 'select' ? Array.from(el.options).map((o) => ({ value: o.value, text: textOf(o) })) : [],
      group_key: isChoice ? groupKeyFor(el) : '',
      controls: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '',
      expanded: el.getAttribute('aria-expanded') === 'true',
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      readonly: !!el.readOnly,
      aria_hidden: !!el.closest('[aria-hidden="true"]'),
      // custom radios/checkboxes hide the input and draw the label (BambooHR Fabric)
      visible: isVisible(el) || (isChoice && (() => {
        const lbl = el.closest('label') || (el.labels && el.labels[0]);
        return !!lbl && isVisible(lbl);
      })()),
      maxlength: parseInt(el.getAttribute('maxlength') || '0', 10) || 0,
      // or a file the site already holds: Eightfold keeps the upload across visits and
      // shows "resume.pdf  Replace  Delete" while the input itself stays empty
      has_files: widget === 'file' ? !!(el.files && el.files.length) || (() => {
        for (let n = el.parentElement, d = 0; n && d < 5; n = n.parentElement, d++) {
          if (n.querySelectorAll('input[type="file"]').length > 1) break;
          const t = textOf(n);
          if (/\.(pdf|docx?|rtf|odt|txt)\b/i.test(t) && /\b(replace|remove|delete)\b/i.test(t)) return true;
        }
        return false;
      })() : false,
      // Workday multiselects keep committed picks as chips beside an empty search box
      chips: (() => {
        const field = el.closest('[data-automation-id^="formField-"], [data-automation-id="multiSelectContainer"]');
        return field ? Array.from(field.querySelectorAll('[data-automation-id="selectedItemList"] li')).map(textOf).filter(Boolean) : [];
      })(),
    };
    // Ant Design selects without search keep a zero-width readonly <input role=combobox>
    // inside the visible .ant-select box (Dayforce): operate the box
    if (widget === 'combobox' && !d.visible) {
      const box = el.closest('.ant-select');
      if (box && isVisible(box)) Object.assign(d, { selector: selectorFor(box), visible: true, readonly: false });
    }
    out.push(d);
  }
  /* Jobvite: no labelled file input, only a "Select" menu button labelled by the
   * "Add Resume*" heading. It is the resume field; fill.py opens its menu for the file. */
  for (const btn of deepAll('button[aria-haspopup]')) {
    if (seen.has(btn) || !isVisible(btn)) continue;
    const name = labelledBy(btn) || btn.getAttribute('aria-label') || '';
    if (!/\b(resume|cv|curriculum)\b/i.test(name)) continue;
    seen.add(btn);
    out.push({
      i: out.length, selector: selectorFor(btn), tag: 'button', type: 'file', widget: 'file',
      role: '', name: '', id: btn.id || '', autocomplete: '', automation_id: '', aria_label: '', placeholder: '',
      label: name, group_label: '', option_label: '', section: sectionOf(btn),
      required: btn.getAttribute('aria-required') === 'true' || /[*✱]\s*$/.test(name),
      value: '', display: '', checked: null, options: [], group_key: '', controls: '', expanded: false,
      disabled: btn.disabled, readonly: false, aria_hidden: false, visible: true, maxlength: 0,
      has_files: false, chips: [],
    });
  }

  /* BambooHR (Fabric): a native <select> kept at zero size behind a
   * <button aria-haspopup> that opens a role=menu. The select is what carries the
   * label and required flag; the button is what a user operates. */
  for (const sel of deepAll('select')) {
    if (isVisible(sel)) continue;
    let btn = null;
    for (let n = sel.parentElement, d = 0; n && d < 3 && !btn; n = n.parentElement, d++) {
      btn = n.querySelector('button[aria-haspopup]:not([aria-haspopup="false"])');
    }
    if (!btn || seen.has(btn) || !isVisible(btn)) continue;
    seen.add(btn);
    out.push({
      i: out.length, selector: selectorFor(btn), tag: 'button', type: 'button', widget: 'combobox',
      role: btn.getAttribute('role') || '', name: sel.getAttribute('name') || '', id: sel.getAttribute('id') || '',
      autocomplete: '', automation_id: '', aria_label: '', placeholder: '',
      label: bestLabel(sel), group_label: '', option_label: '', section: sectionOf(sel),
      required: !!(sel.required || /[*✱]\s*$/.test(bestLabel(sel))),
      value: '', display: /^[–—-]*\s*select\s*[–—-]*$/i.test(textOf(btn)) ? '' : textOf(btn),
      checked: null, options: Array.from(sel.options).filter((o) => textOf(o)).map((o) => ({ value: o.value, text: textOf(o) })),
      group_key: '', controls: '', expanded: btn.getAttribute('aria-expanded') === 'true',
      disabled: btn.getAttribute('aria-disabled') === 'true', readonly: false, aria_hidden: false,
      visible: true, maxlength: 0, has_files: false, chips: [],
    });
  }
  return JSON.stringify({ url: location.href, fields: out });
}
