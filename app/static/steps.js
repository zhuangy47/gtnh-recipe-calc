// The build order, drawn.  The server sends the steps, the columns they fall
// into, and which of them you can start; this file puts them somewhere and
// repaints them after a tick.
//
// It works out no order of its own.  Layer, prerequisites, what a tick
// unlocked -- all of that comes back from `order.build_order` on the server,
// because it is model logic and a second copy of it in JavaScript is exactly
// the drift the route's two views were built to avoid.  What lives here is
// arithmetic: x is the column, y is a position inside it chosen to keep the
// arrows from crossing, and a camera over the top.
(function () {
  var view = document.getElementById('sview');
  var list = document.getElementById('slist');
  if (!view || !list) return;
  var world = document.getElementById('sworld');
  var svg = document.getElementById('sedges');
  var pop = document.getElementById('spop');
  var pid = view.dataset.pid;

  // Graph or list.  Declared up here because `goTo` below branches on it;
  // `setMode` at the foot of the file is what sets it.
  var mode = 'graph';

  var NODE_W = 280, NODE_H = 52;   // must match .snode in app.css
  var COL = 340, ROW = 64, HEAD = 34, PAD = 20;
  var MIN_SC = 0.08;

  // -- the graph, read back off the DOM ------------------------------------
  var order = [], by = {}, cols = [];
  world.querySelectorAll('.snode').forEach(function (el) {
    var needs = (el.dataset.needs || '').split(' ').filter(Boolean);
    var n = { el: el, id: el.dataset.step, layer: +el.dataset.layer,
              needs: needs, feeds: [], x: 0, y: 0, i: 0 };
    by[n.id] = n;
    order.push(n);
    while (cols.length <= n.layer) cols.push([]);
    n.i = cols[n.layer].length;
    cols[n.layer].push(n);
  });
  order.forEach(function (n) {
    n.needs = n.needs.filter(function (m) { return by[m]; });
    n.needs.forEach(function (m) { by[m].feeds.push(n.id); });
  });
  var heads = Array.from(world.querySelectorAll('.scolhead'));

  // -- layout --------------------------------------------------------------
  // Columns are given, so the only choice is the order inside one.  Sweep the
  // barycentre a few times -- put each box level with the average of the boxes
  // it is joined to -- which is the standard cheap answer and is plenty for a
  // route-sized graph.  Ties keep the server's order, which groups a column by
  // machine, so the sweep only ever tidies.
  function sweep(fromLeft) {
    var start = fromLeft ? 1 : cols.length - 2;
    var stop = fromLeft ? cols.length : -1;
    var stride = fromLeft ? 1 : -1;
    for (var L = start; L !== stop; L += stride) {
      var col = cols[L];
      col.forEach(function (n) {
        var near = fromLeft ? n.needs : n.feeds;
        var t = 0, c = 0;
        near.forEach(function (m) {
          var o = by[m];
          if (o && o.layer !== L) { t += o.i; c++; }
        });
        n.bary = c ? t / c : n.i;
      });
      col.sort(function (a, b) { return (a.bary - b.bary) || (a.i - b.i); });
      col.forEach(function (n, i) { n.i = i; });
    }
  }

  var W = 0, H = 0;

  function layout() {
    var tallest = cols.reduce(function (m, c) { return Math.max(m, c.length); }, 0);
    H = PAD + HEAD + tallest * ROW + PAD;
    W = PAD + Math.max(1, cols.length) * COL;
    cols.forEach(function (col, L) {
      // Columns are centred on each other: the last column is usually one box
      // -- the thing you are building -- and pinned to the top it would sit
      // opposite nothing.
      var top = PAD + HEAD + (tallest - col.length) * ROW / 2;
      col.forEach(function (n, i) {
        n.i = i;
        n.x = PAD + L * COL;
        n.y = top + i * ROW;
        n.el.style.transform = 'translate(' + n.x + 'px,' + n.y + 'px)';
      });
      var h = heads[L];
      if (h) {
        h.style.transform = 'translate(' + (PAD + L * COL) + 'px,' + PAD + 'px)';
      }
    });
    edges();
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    world.style.width = W + 'px';
    world.style.height = H + 'px';
  }

  function edges() {
    var lines = [];
    order.forEach(function (n) {
      n.needs.forEach(function (m) {
        var p = by[m];
        if (!p) return;
        var x1 = p.x + NODE_W, y1 = p.y + NODE_H / 2;
        var x2 = n.x, y2 = n.y + NODE_H / 2, c = Math.max(30, (x2 - x1) * 0.45);
        lines.push('<path data-from="' + m + '" data-to="' + n.id + '"' +
                   (p.el.classList.contains('is-done') ? ' class="done"' : '') +
                   ' d="M' + x1 + ',' + y1 + 'C' + (x1 + c) + ',' + y1 + ' ' +
                   (x2 - c) + ',' + y2 + ' ' + x2 + ',' + y2 + '"/>');
      });
    });
    svg.innerHTML = lines.join('');
    lightEdges(hot);
  }

  for (var pass = 0; pass < 4; pass++) { sweep(true); sweep(false); }

  // -- pan and zoom --------------------------------------------------------
  // Deliberately its own copy of the route graph's camera rather than a shared
  // one: that file's is entangled with folding and with a panel pinned to a
  // box, and untangling working code to save forty lines here would be the
  // more expensive change.
  var tx = 0, ty = 0, sc = 1, picking = null;

  function apply() {
    world.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + sc + ')';
    document.getElementById('szoom').textContent = Math.round(sc * 100) + '%';
    if (picking && !pop.hidden) placePop(picking);
  }

  function zoom(f, ax, ay) {
    var next = Math.min(2, Math.max(MIN_SC, sc * f));
    if (next === sc) return;
    var r = view.getBoundingClientRect();
    var px = (ax === undefined ? r.width / 2 : ax - r.left);
    var py = (ay === undefined ? r.height / 2 : ay - r.top);
    tx = px - (px - tx) * (next / sc);
    ty = py - (py - ty) * (next / sc);
    sc = next;
    apply();
  }

  function fit() {
    var r = view.getBoundingClientRect();
    sc = Math.max(MIN_SC, Math.min((r.width - 24) / W, (r.height - 24) / H, 1));
    tx = 12;
    ty = Math.max(12, (r.height - H * sc) / 2);
    apply();
  }

  function onscreen(n) {
    var r = view.getBoundingClientRect();
    var x = n.x * sc + tx, y = n.y * sc + ty;
    return x >= 0 && y >= 0 &&
           x + NODE_W * sc <= r.width && y + NODE_H * sc <= r.height;
  }

  function centre(n) {
    var r = view.getBoundingClientRect();
    tx = r.width / 2 - (n.x + NODE_W / 2) * sc;
    ty = r.height / 2 - (n.y + NODE_H / 2) * sc;
    apply();
  }

  function flash(n) {
    n.el.classList.remove('flash');
    void n.el.offsetWidth;
    n.el.classList.add('flash');
  }

  view.addEventListener('wheel', function (ev) {
    if (ev.target.closest('.gpop')) {
      if (!ev.target.closest('.gpop-body')) ev.preventDefault();
      return;
    }
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) {
      zoom(Math.pow(0.995, ev.deltaY), ev.clientX, ev.clientY);
    } else {
      tx -= ev.deltaX;
      ty -= ev.deltaY;
      apply();
    }
  }, { passive: false });

  // An <img> is a native drag source and takes the pointer away mid-pan; the
  // route graph learned that the hard way.  Refuse the gesture for the drawing.
  world.addEventListener('dragstart', function (ev) { ev.preventDefault(); });

  var drag = null, down = null, SLOP = 5;
  view.addEventListener('pointerdown', function (ev) {
    down = null;
    drag = null;
    if (ev.button !== 0) return;
    if (ev.target.closest('button, select, input, .gpop')) return;
    down = { x: ev.clientX, y: ev.clientY };
    drag = { x: ev.clientX, y: ev.clientY, tx: tx, ty: ty, held: false };
  });
  document.addEventListener('pointermove', function (ev) {
    if (!drag) return;
    var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!drag.held) {
      // Capture only once the gesture really is a drag: taking it on
      // pointerdown re-targets the click to .gview and no box is ever clicked.
      if (Math.abs(dx) + Math.abs(dy) <= SLOP) return;
      drag.held = true;
      view.setPointerCapture(ev.pointerId);
      view.classList.add('dragging');
      // A pan slides the boxes under a pointer that is not moving relative to
      // the screen, so no mouseover fires and the highlight would go on
      // pointing at whatever used to be there.
      unlight();
    }
    tx = drag.tx + dx;
    ty = drag.ty + dy;
    apply();
  });
  function endDrag() {
    drag = null;
    view.classList.remove('dragging');
  }
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
  view.addEventListener('click', function (ev) {
    if (!down) return;
    var far = Math.abs(ev.clientX - down.x) + Math.abs(ev.clientY - down.y) > SLOP;
    down = null;
    if (far) {
      ev.preventDefault();
      ev.stopPropagation();
      return;
    }
    if (!pop.hidden && !ev.target.closest('.gpop, .snode')) closePop();
  }, true);
  view.addEventListener('scroll', function () {
    view.scrollTop = 0;
    view.scrollLeft = 0;
  });

  // -- what a box says when you open it ------------------------------------
  // The panel is the list row's own detail, moved.  Rendering it twice would
  // be two things to keep true; this way the drawing literally shows you what
  // the list says.
  function placePop(n) {
    var r = view.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var right = n.x * sc + tx + NODE_W * sc + 12;
    var left = n.x * sc + tx - w - 12;
    var x = (right + w > r.width - 8 && left >= 8) ? left : right;
    var y = n.y * sc + ty + NODE_H * sc / 2 - h / 2;
    pop.style.left = Math.max(8, Math.min(x, r.width - w - 8)) + 'px';
    pop.style.top = Math.max(8, Math.min(y, r.height - h - 8)) + 'px';
  }

  function closePop() {
    pop.hidden = true;
    pop.innerHTML = '';
    if (picking) picking.el.classList.remove('picking');
    picking = null;
  }

  function openPop(n) {
    var src = list.querySelector('[data-step="' + n.id + '"] .sdet');
    if (!src) return;
    if (picking && picking !== n) picking.el.classList.remove('picking');
    picking = n;
    n.el.classList.add('picking');
    var name = n.el.querySelector('.gname');
    pop.innerHTML = '<div class="gpop-head row"><b>' +
      (name ? name.textContent : '') +
      '</b><span class="grow"></span>' +
      '<button type="button" class="small ghost gpop-close">close</button></div>' +
      '<div class="gpop-body"></div>';
    pop.querySelector('.gpop-body').appendChild(src.cloneNode(true));
    pop.hidden = false;
    placePop(n);
  }

  pop.addEventListener('click', function (ev) {
    if (ev.target.closest('.gpop-close')) closePop();
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !pop.hidden) closePop();
  });

  world.addEventListener('click', function (ev) {
    var hit = ev.target.closest('.shit');
    if (!hit) return;
    var n = by[hit.closest('.snode').dataset.step];
    if (picking === n && !pop.hidden) { closePop(); return; }
    if (!onscreen(n)) centre(n);
    openPop(n);
  });
  world.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var hit = ev.target.closest('.shit');
    if (!hit) return;
    ev.preventDefault();
    openPop(by[hit.closest('.snode').dataset.step]);
  });

  // Hovering a box lights what feeds it and what it feeds -- one hop each way,
  // which is the question a build order is asked ("can I do this yet, and what
  // does it get me"), and unlike the route's chain-to-the-root it stays legible
  // on a graph where everything eventually joins up.
  var hot = null;

  // Sweeps the DOM rather than replaying a list of what was lit.  The list was
  // bookkeeping, and bookkeeping is what leaves a stale highlight behind the
  // one time it gets out of step; asking the document what is lit cannot.  It
  // only runs when something is lit, so the cost is nil on an idle canvas.
  function unlight() {
    if (!hot) return;
    hot = null;
    world.classList.remove('hovering');
    world.querySelectorAll('.snode.lit, .snode.hot').forEach(function (el) {
      el.classList.remove('lit', 'hot');
    });
    svg.querySelectorAll('path.lit').forEach(function (p) {
      p.classList.remove('lit');
    });
  }

  // The hovered node's own edges, raised to the end of the <svg> so they draw
  // over the rest rather than under whichever hundred were emitted after them.
  // Called again after every rebuild of the edges, because `edges()` replaces
  // the markup and would otherwise drop the highlight out from under a pointer
  // that has not moved -- ticking a step off while hovering did exactly that.
  function lightEdges(n) {
    if (!n) return;
    svg.querySelectorAll('path').forEach(function (p) {
      if (p.getAttribute('data-to') === n.id ||
          p.getAttribute('data-from') === n.id) {
        p.classList.add('lit');
        svg.appendChild(p);
      }
    });
  }

  // On the view, not on the world, and it answers for the misses as well as the
  // hits.  Listening only for boxes meant nothing at all happened when the
  // pointer left one into the space between two columns -- and on this drawing
  // that space is most of the canvas, so the fade simply stayed put until you
  // left the whole thing.  `mouseleave` fires far too late to be the only way
  // out.
  view.addEventListener('mouseover', function (ev) {
    var box = ev.target.closest('.snode');
    if (!box) {
      unlight();
      return;
    }
    var n = by[box.dataset.step];
    if (!n || hot === n) return;
    unlight();
    hot = n;
    n.el.classList.add('hot');
    [n].concat(n.needs.map(function (m) { return by[m]; }),
               n.feeds.map(function (m) { return by[m]; }))
      .forEach(function (m) {
        if (m) m.el.classList.add('lit');
      });
    // Last, so the fade cannot flash across the boxes before they are marked.
    world.classList.add('hovering');
    lightEdges(n);
  });
  view.addEventListener('mouseleave', unlight);

  // -- the next thing you can do -------------------------------------------
  var nextBtn = document.getElementById('snext');
  var ready = [], at = -1;

  function collectReady() {
    ready = order.filter(function (n) {
      return n.el.classList.contains('is-ready');
    });
    at = -1;
    nextBtn.hidden = !ready.length;
  }
  collectReady();

  nextBtn.addEventListener('click', function () {
    if (!ready.length) return;
    at = (at + 1) % ready.length;
    goTo(ready[at].id, mode === 'graph');
  });

  function goTo(sid, quiet) {
    if (mode === 'graph') {
      var n = by[sid];
      if (!n) return;
      centre(n);
      flash(n);
      if (!quiet) openPop(n);
      return;
    }
    var row = list.querySelector('details[data-step="' + sid + '"]');
    if (!row) return;
    row.open = true;
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    row.classList.add('flash');
    setTimeout(function () { row.classList.remove('flash'); }, 900);
  }

  document.addEventListener('click', function (ev) {
    var g = ev.target.closest('[data-goto]');
    if (!g) return;
    ev.preventDefault();
    goTo(g.dataset.goto);
  });

  // -- ticking a step off --------------------------------------------------
  // The form posts on its own with JavaScript off; here it is intercepted so
  // the answer arrives without losing the camera.  The response is the whole
  // state, re-derived server-side, and it is applied wholesale -- nothing is
  // guessed at from what was clicked.
  function repaint(data) {
    Object.keys(data.steps).forEach(function (sid) {
      var s = data.steps[sid];
      document.querySelectorAll('[data-step="' + sid + '"]').forEach(function (el) {
        el.classList.remove('is-ready', 'is-blocked', 'is-done');
        el.classList.add('is-' + s.state);
        var flag = el.querySelector('.sflag');
        if (flag) {
          flag.hidden = !s.redo;
          // Written by the server: counts or stacks is its call, not this
          // file's, and the two shapes must not disagree about it.
          flag.textContent = s.redo_label + ' more';
        }
      });
      document.querySelectorAll('[data-tick="' + sid + '"]').forEach(function (f) {
        f.querySelector('input[name=state]').value = s.done ? 'todo' : 'done';
        var b = f.querySelector('.tickbtn');
        b.innerHTML = s.done ? '\u2713' : '';
        b.setAttribute('aria-label', s.done ? 'done' : 'not done yet');
        b.title = s.done
          ? 'you have done this \u2014 click to put it back on the list'
          : 'mark this done, and light up whatever it unlocks';
      });
    });
    var c = data.counts;
    document.getElementById('scount-done').textContent = c.done;
    document.getElementById('scount-ready').textContent = c.ready + ' you can do now';
    document.getElementById('scount-blocked').textContent = c.blocked + ' waiting';
    var stale = document.getElementById('scount-stale');
    stale.hidden = !c.stale;
    stale.textContent = c.stale + ' need more';
    document.getElementById('sbarfill').style.width =
      (c.total ? 100 * c.done / c.total : 0) + '%';
    edges();
    collectReady();
  }

  document.addEventListener('submit', function (ev) {
    var form = ev.target.closest('form.stick');
    if (!form) return;
    ev.preventDefault();
    var body = new FormData(form);
    body.append('fmt', 'json');
    fetch(form.action + '?fmt=json', { method: 'POST', body: body })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status);
        return r.json();
      })
      .then(repaint)
      .catch(function () { form.submit(); });   // fall back to the plain post
  });

  // -- which shape you are looking at --------------------------------------
  // Kept for the tab, like the route graph's depth: it is a question about the
  // picture, not about the plan.
  var MKEY = 'smode:' + pid;
  var tabs = document.getElementById('smode');

  function setMode(m, save) {
    mode = m;
    tabs.querySelectorAll('a').forEach(function (a) {
      a.classList.toggle('on', a.dataset.mode === m);
    });
    document.querySelectorAll('.sonly-graph').forEach(function (el) {
      el.hidden = m !== 'graph';
    });
    list.hidden = m !== 'list';
    if (save) { try { sessionStorage.setItem(MKEY, m); } catch (e) {} }
    if (m === 'graph') {
      layout();
      fit();
      world.classList.remove('pending');
    } else {
      closePop();
    }
  }

  tabs.addEventListener('click', function (ev) {
    var a = ev.target.closest('a[data-mode]');
    if (!a) return;
    ev.preventDefault();
    setMode(a.dataset.mode, true);
  });

  document.getElementById('sin').addEventListener('click', function () { zoom(1.25); });
  document.getElementById('sout').addEventListener('click', function () { zoom(0.8); });
  document.getElementById('sfit').addEventListener('click', fit);

  var saved = null;
  try { saved = sessionStorage.getItem(MKEY); } catch (e) {}
  setMode(saved === 'list' ? 'list' : 'graph', false);

  // Arriving from a plain form post, which carries #<step id>.
  var want = location.hash.slice(1);
  if (want && by[want]) goTo(want);
  // The viewport height is not necessarily settled at end-of-body, and `fit`
  // reads it; correct once the frame has been laid out.
  requestAnimationFrame(function () { if (mode === 'graph') fit(); });
})();
