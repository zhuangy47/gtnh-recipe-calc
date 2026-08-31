// Two small things: a type-ahead item picker, and the icon that follows a
// set-valued slot's select.  Everything else is a form post and a server
// re-render, because a plan stores decisions and every view is recomputed from
// them anyway.
(function () {
  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  function wire(input) {
    var box = input.closest('.searchbox');
    var results = box.querySelector('.results');
    var hidden = box.querySelector('.js-value');
    var chosen = box.querySelector('.chosen');

    function clear() { results.innerHTML = ''; }

    var run = debounce(function () {
      var q = input.value.trim();
      if (q.length < 2) { clear(); return; }
      var url = '/search?fragment=1&limit=25&q=' + encodeURIComponent(q);
      if (input.dataset.kind) url += '&kind=' + input.dataset.kind;
      fetch(url).then(function (r) { return r.text(); }).then(function (html) {
        results.innerHTML = html;
      }).catch(function () { clear(); });
    }, 160);

    input.addEventListener('input', run);
    input.addEventListener('focus', run);

    results.addEventListener('click', function (ev) {
      var b = ev.target.closest('button[data-ix]');
      if (!b) return;
      ev.preventDefault();
      hidden.value = b.dataset.ix;
      input.value = b.dataset.name;
      chosen.textContent = b.dataset.gid;
      clear();
    });

    document.addEventListener('click', function (ev) {
      if (!box.contains(ev.target)) clear();
    });
  }

  document.querySelectorAll('input.js-search').forEach(wire);

  // A substitutable slot is a <select>, and an <option> cannot carry a picture,
  // so the icon lives next to the select and is repointed on every change.  The
  // listener is delegated because the graph's choice panel injects its selects
  // long after this file has run.
  document.addEventListener('change', function (ev) {
    var sel = ev.target;
    if (!sel.matches || !sel.matches('select.js-picker')) return;
    var img = sel.parentNode.querySelector('img.js-pic');
    var opt = sel.options[sel.selectedIndex];
    if (!img || !opt || !opt.dataset.icon) return;
    img.src = opt.dataset.icon;
    img.title = opt.dataset.name || '';
  });

  // Rows added on demand in the hand-entered recipe editor.
  document.querySelectorAll('[data-add-row]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var tpl = document.querySelector(btn.dataset.addRow);
      var host = document.querySelector(btn.dataset.into);
      var n = host.children.length + 1;
      var html = tpl.innerHTML.replace(/__N__/g, String(n + 100));
      var div = document.createElement('div');
      div.innerHTML = html;
      var node = div.firstElementChild;
      host.appendChild(node);
      node.querySelectorAll('input.js-search').forEach(wire);
    });
  });
})();
