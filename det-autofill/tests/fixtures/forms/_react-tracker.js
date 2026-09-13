/*
 * A faithful re-implementation of React DOM's inputValueTracking.
 * React installs an *instance-level* `value` accessor that shadows the
 * prototype one and records the last value it saw. On every `input` event it
 * compares node.value against that record; if they are equal it concludes
 * nothing changed and never calls onChange.
 *
 * Consequence: `el.value = 'x'` goes through the instance setter, updates the
 * record, and is therefore invisible to React. Only the prototype setter
 * (Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set)
 * writes the DOM while leaving the record stale.
 *
 * Fixtures that use this mirror the real failure mode, so the test proves the
 * filler works on React-controlled forms and not just on plain inputs.
 */
window.trackLikeReact = function (el) {
  var descriptor = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value');
  var recorded = '' + el.value;
  Object.defineProperty(el, 'value', {
    configurable: true,
    get: function () { return descriptor.get.call(this); },
    set: function (v) { recorded = '' + v; descriptor.set.call(this, v); }
  });
  function updateIfChanged() {
    var next = descriptor.get.call(el);
    if (next === recorded) return false;
    recorded = next;
    return true;
  }
  // React listens at the document root, not on the node.
  document.addEventListener('input', function (e) {
    if (e.target !== el) return;
    if (updateIfChanged()) el.setAttribute('data-react-state', descriptor.get.call(el));
  }, true);
  document.addEventListener('change', function (e) {
    if (e.target !== el) return;
    if (updateIfChanged()) el.setAttribute('data-react-state', descriptor.get.call(el));
  }, true);
};
