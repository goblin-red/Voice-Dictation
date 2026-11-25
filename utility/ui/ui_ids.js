/* Stable interface IDs. Static source: ui_ids.json; dynamic families: docs/UI-IDS.md. */
(() => {
  const copies = new WeakMap();
  const selector = 'input:not([type="hidden"]), select, textarea, button, a[href], summary, .row > label, .seg-block > label';
  const encoded = value => encodeURIComponent(value);
  function identity(el) {
    if (el.dataset.goblinId) return el.dataset.goblinId;
    const parent = el.parentElement;
    const prefix = parent && parent !== document.body ? identity(parent) : 'voice:dynamic';
    const semantic = el.id || el.dataset.key || el.dataset.act || el.dataset.mirror || el.dataset.i18n || el.getAttribute('name');
    const siblings = el.parentElement ? [...el.parentElement.children].filter(x => x.tagName === el.tagName && !x.classList.contains('ui-id-copy')) : [];
    const local = semantic ? encoded(semantic) : el.tagName.toLowerCase() + ':' + siblings.indexOf(el);
    el.dataset.goblinId = prefix + '/' + local;
    return el.dataset.goblinId;
  }
  async function copy(text) {
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(text); return; } catch (_) {}
    }
    const field = document.createElement('textarea');
    field.dataset.uiIdHelper = 'true';
    field.value = text;
    field.style.cssText = 'position:fixed;left:-10000px;top:0';
    const previous = document.activeElement;
    document.body.append(field); field.select();
    const ok = document.execCommand('copy');
    field.remove(); previous?.focus({preventScroll:true});
    if (!ok) throw new Error('clipboard unavailable');
  }
  function decorate() {
    document.querySelectorAll(selector).forEach(el => {
      if (el.closest('.ui-id-copy') || el.dataset.uiIdHelper || el.closest('option')) return;
      // A label and its control have distinct stable IDs; both remain discoverable.
      const id = identity(el);
      if (copies.get(el)?.isConnected) return;
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ui-id-copy'; b.textContent = '⧉';
      b.title = 'Copy identifier: ' + id;
      b.setAttribute('aria-label', b.title);
      // Native labels can extend :hover to their associated button. Use the
      // pointer coordinates so only the copy icon's own box is highlighted.
      b.addEventListener('pointermove', event => {
        const r = b.getBoundingClientRect();
        b.classList.toggle('pointer-over', event.clientX >= r.left && event.clientX < r.right && event.clientY >= r.top && event.clientY < r.bottom);
      });
      b.addEventListener('pointerleave', () => b.classList.remove('pointer-over'));
      b.onclick = async event => {
        event.preventDefault(); event.stopPropagation();
        try { await copy(id); b.textContent = '✓'; b.title = 'Copied: ' + id; }
        catch (_) { b.textContent = '!'; b.title = 'Could not copy: ' + id; }
        setTimeout(() => { b.textContent = '⧉'; b.title = 'Copy identifier: ' + id; }, 1800);
      };
      if (el.matches('summary, label')) el.append(b);
      else el.insertAdjacentElement('afterend', b);
      copies.set(el, b);
    });
  }
  // Metadata only: no values from fields, transcripts, keys, or messages.
  window.goblinUiIds = () => [...document.querySelectorAll('[data-goblin-id]')].map(el => ({
    id:el.dataset.goblinId, tag:el.tagName.toLowerCase(), source:'utility/ui/ui.html',
    control:el.id || null, action:el.dataset.act || null,
  }));
  let scheduled = false;
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => { scheduled = false; decorate(); }, 100);
  });
  decorate();
  observer.observe(document.body, {childList:true, subtree:true});
})();
