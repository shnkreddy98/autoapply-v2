  /* Same-origin frames. iCIMS renders the whole application inside
   * <iframe id="icims_content_iframe">, a separate document that document.querySelector
   * in the top page never reaches; its own URL redirects back to the wrapper when
   * opened directly. Selectors into a frame use Playwright's own frame hop,
   * "iframe#x >> internal:control=enter-frame >> #field", so the same string works for
   * page.locator() / MCP targets and for deepQuery() below. Cross-origin frames stay
   * out: their documents cannot be read. Spliced into every page probe. */
  const FRAME_HOP = ' >> internal:control=enter-frame >> ';

  function frameSelector(f, doc) {
    const one = (s) => { try { return doc.querySelectorAll(s).length === 1; } catch (e) { return false; } };
    if (f.id && one('#' + CSS.escape(f.id))) return '#' + CSS.escape(f.id);
    const name = f.getAttribute('name');
    if (name && one(`iframe[name="${CSS.escape(name)}"]`)) return `iframe[name="${CSS.escape(name)}"]`;
    return `iframe >> nth=${Array.from(doc.querySelectorAll('iframe')).indexOf(f)}`;
  }

  /* [{doc, prefix}] for the page and every readable frame big enough to hold a form. */
  function frameDocs() {
    const out = [];
    const walk = (doc, prefix, depth) => {
      out.push({ doc, prefix });
      if (depth >= 3) return;
      for (const f of doc.querySelectorAll('iframe')) {
        let inner = null;
        try { inner = f.contentDocument; } catch (e) { continue; }
        const r = f.getBoundingClientRect();
        if (!inner || !inner.body || r.width < 40 || r.height < 40) continue;
        walk(inner, prefix + frameSelector(f, doc) + FRAME_HOP, depth + 1);
      }
    };
    walk(document, '', 0);
    return out;
  }

  function framePrefix(doc) {
    if (doc === document) return '';
    const hit = frameDocs().find((x) => x.doc === doc);
    return hit ? hit.prefix : '';
  }

  function queryIn(doc, part) {
    const m = part.match(/^(.*) >> nth=(\d+)$/);
    try { return m ? doc.querySelectorAll(m[1])[Number(m[2])] || null : doc.querySelector(part); } catch (e) { return null; }
  }

  /* document.querySelector that follows frame hops. */
  function deepQuery(selector) {
    const parts = String(selector).split(FRAME_HOP);
    let doc = document;
    for (let i = 0; i < parts.length - 1; i++) {
      const f = queryIn(doc, parts[i]);
      try { doc = f && f.contentDocument; } catch (e) { doc = null; }
      if (!doc) return null;
    }
    return queryIn(doc, parts[parts.length - 1]);
  }

  /* querySelectorAll over the page and its frames. */
  function deepAll(css) {
    return frameDocs().flatMap(({ doc }) => Array.from(doc.querySelectorAll(css)));
  }
