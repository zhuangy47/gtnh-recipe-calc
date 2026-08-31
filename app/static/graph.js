// The route, drawn.  The server sends exactly the rows the list view sends --
// same ids, same quantities, same buttons -- and this file does the one thing
// the server cannot: put them somewhere.
//
// Layout is arithmetic, not model logic, which is why it is allowed to live
// here.  x is depth and y is leaf order, so a tree draws with no crossings at
// all and no algorithm worth the name: place the children, sit between the
// first and the last.  Folding a branch is a question about the picture, so it
// is answered without a round trip and deliberately not saved.
(function () {
  var view = document.getElementById('gview');
  if (!view) return;
  var world = document.getElementById('gworld');
  var svg = document.getElementById('gedges');

  var NODE_W = 250, NODE_H = 46;   // must match .gnode in app.css
  var COL = 300, ROW = 58, GAP = 34, PAD = 20;
  var MIN_SC = 0.05;   // a 133-leaf route only fits at "shape only" scale

  // -- the tree, read back off the DOM ------------------------------------
  // `tree_rows` emits parents before children, so one pass builds it.
  var order = [], by = {}, roots = [];
  world.querySelectorAll('.gnode').forEach(function (el) {
    var n = { el: el, id: el.dataset.id, pid: el.dataset.parent || null,
              kind: el.dataset.kind, kids: [], folded: false,
              x: 0, y: 0, shown: true, depth: +el.dataset.depth, below: 0 };
    by[n.id] = n;
    order.push(n);
  });
  order.forEach(function (n) {
    var p = n.pid ? by[n.pid] : null;
    if (p) { p.kids.push(n); } else { roots.push(n); }
  });
  if (!order.length) return;

  function countBelow(n) {
    var t = 0;
    n.kids.forEach(function (k) { t += 1 + countBelow(k); });
    n.below = t;
    return t;
  }
  roots.forEach(countBelow);

  // -- layout --------------------------------------------------------------
  var W = 0, H = 0;

  function layout() {
    var cur = PAD;
    W = 0;
    order.forEach(function (n) { n.shown = false; });

    function place(n, depth) {
      n.shown = true;
      n.x = PAD + depth * COL;
      if (n.folded || !n.kids.length) {
        n.y = cur;
        cur += ROW;
      } else {
        n.kids.forEach(function (k) { place(k, depth + 1); });
        n.y = (n.kids[0].y + n.kids[n.kids.length - 1].y) / 2;
      }
      if (n.x + NODE_W > W) W = n.x + NODE_W;
    }
    roots.forEach(function (r) { place(r, 0); cur += GAP; });
    H = cur - GAP + PAD;

    var lines = [];
    order.forEach(function (n) {
      var el = n.el;
      if (!n.shown) { el.style.display = 'none'; return; }
      el.style.display = '';
      el.style.transform = 'translate(' + n.x + 'px,' + n.y + 'px)';
      var p = n.pid ? by[n.pid] : null;
      if (p && p.shown && !p.folded) {
        var x1 = p.x + NODE_W, y1 = p.y + NODE_H / 2;
        var x2 = n.x, y2 = n.y + NODE_H / 2, c = (x2 - x1) * 0.5;
        lines.push('<path data-to="' + n.id + '" d="M' + x1 + ',' + y1 +
                   'C' + (x1 + c) + ',' + y1 + ' ' + (x2 - c) + ',' + y2 +
                   ' ' + x2 + ',' + y2 + '"/>');
      }
    });
    svg.innerHTML = lines.join('');
    svg.setAttribute('width', W + PAD);
    svg.setAttribute('height', H);
    world.style.width = (W + PAD) + 'px';
    world.style.height = H + 'px';
  }

  function fold(n, want) {
    if (!n.kids.length) return;
    n.folded = want;
    var tog = n.el.querySelector('.gtog');
    if (tog) {
      tog.textContent = want ? '+' : '−';
      tog.title = want ? 'unfold this branch' : 'fold this branch away';
    }
    // Say what was folded away, so a "+" is never a mystery.
    var tag = n.el.querySelector('.ghid');
    if (tag) {
      tag.hidden = !want;
      tag.textContent = want ? '+' + n.below : '';
    }
    n.el.classList.toggle('folded', want);
  }

  function foldBelow(depth) {
    order.forEach(function (n) { fold(n, depth >= 0 && n.depth >= depth); });
    layout();
  }

  var maxDepth = order.reduce(function (m, n) { return Math.max(m, n.depth); }, 0);

  // A 280-box route is 7,700px tall and its root sits at the vertical middle
  // of that, so "expanded, scrolled to the top-left" opens on the middle of a
  // branch with no context.  Open folded to the deepest level that still fits
  // in a couple of screens instead: the shape is legible and unfolding is one
  // click.  Nothing here touches the plan.
  function heightAt(depth) {
    // Folded at `depth`, a row is spent on every node at exactly that depth
    // and on every leaf above it.  Same arithmetic as layout(), without the
    // drawing.
    var rows = 0;
    order.forEach(function (n) {
      if (n.depth === depth || (n.depth < depth && !n.kids.length)) rows++;
    });
    return PAD + rows * ROW + (roots.length - 1) * GAP + PAD;
  }

  function openingDepth(limit) {
    var best = 0;
    for (var d = 1; d <= maxDepth; d++) {
      if (heightAt(d) > limit) break;  // it only grows with d, so stop at the
      best = d;                        // first depth that overflows
    }
    if (best >= maxDepth) return -1;   // the whole route fits: show all of it
    return best || 1;                  // never less than the roots and their inputs
  }

  // The depth you picked, remembered for the tab.  Every decision taken in
  // the picker is a form post and a fresh page, so recomputing the opening
  // depth on the way back throws away the view you were working at: ask for
  // all of it, choose a recipe, and it comes back folded around the box you
  // had just changed.  Kept in sessionStorage rather than in the plan, because
  // folding still says nothing about the route -- shut the tab and the drawing
  // opens at whatever depth fits again.
  var depthSel = document.getElementById('gdepth');
  var DKEY = 'gdepth:' + view.dataset.pid;

  function savedDepth() {
    var v;
    try { v = sessionStorage.getItem(DKEY); } catch (e) { return null; }
    if (v === null) return null;
    var d = parseInt(v, 10);
    if (isNaN(d)) return null;
    // A decision can shorten the route, and the select only offers the depths
    // this drawing has; deeper than the deepest means `all` anyway.
    return d < 0 ? -1 : Math.max(1, Math.min(d, maxDepth));
  }

  // The two depth controls, and the only place the choice is written down.
  function setDepth(d) {
    depthSel.value = String(d);
    try { sessionStorage.setItem(DKEY, String(d)); } catch (e) {}
    keepSpot(function () { foldBelow(d); });
  }

  // -- pan and zoom --------------------------------------------------------
  var tx = 0, ty = 0, sc = 1;
  // Declared here because apply() below reports the panel's position, and the
  // picker that owns them is set up further down.
  var pop = null, picking = null, placePop = function () {};

  function apply() {
    world.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + sc + ')';
    document.getElementById('gzoom').textContent = Math.round(sc * 100) + '%';
    // The panel is pinned to a box, so it travels with it.
    if (picking && pop && !pop.hidden) placePop(picking);
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
    sc = Math.max(MIN_SC, Math.min((r.width - 24) / (W + PAD),
                                   (r.height - 24) / H, 1));
    tx = 12;
    ty = Math.max(12, (r.height - H * sc) / 2);
    apply();
  }

  function centre(n) {
    var r = view.getBoundingClientRect();
    tx = r.width / 2 - (n.x + NODE_W / 2) * sc;
    ty = r.height / 2 - (n.y + NODE_H / 2) * sc;
    apply();
  }

  // The root against the left edge, vertically where it actually is -- which
  // in a dendrogram is the middle of everything below it, not the top.
  function home() {
    var r = view.getBoundingClientRect();
    sc = 1;
    tx = 12;
    ty = r.height / 2 - (roots[0].y + NODE_H / 2);
    apply();
  }

  // Re-folding without losing your place.  A fold moves everything, not just
  // the branch: a parent sits between its children, so hiding one three levels
  // down slides the rows above it too, and a camera left where it was ends up
  // pointing at whatever moved into that spot -- from most places in a tall
  // route, the top.  So note the box nearest the middle of the view, and put
  // it back where it was afterwards.  If the fold swallowed it, the branch it
  // collapsed into stands in for it, which is where you would look for it.
  function nearestToMiddle() {
    var r = view.getBoundingClientRect();
    var cx = r.width / 2, cy = r.height / 2, best = null, bd = Infinity;
    order.forEach(function (n) {
      if (!n.shown) return;
      var d = Math.abs(n.x * sc + tx + NODE_W * sc / 2 - cx) +
              Math.abs(n.y * sc + ty + NODE_H * sc / 2 - cy);
      if (d < bd) { bd = d; best = n; }
    });
    return best;
  }

  // Depth is the one thing folding cannot change, so x only moves when the
  // anchor itself was folded away and a shallower box took its place -- and a
  // box eight columns to the left, held where the deep one was, can carry the
  // whole drawing off the side of the view.  Only that case is corrected, and
  // only as far as the drawing's own edge.
  function clampX() {
    var r = view.getBoundingClientRect();
    var edge = r.width - (W + PAD) * sc - 12;
    tx = Math.min(Math.max(tx, Math.min(12, edge)), Math.max(12, edge));
  }

  function keepSpot(change, on) {
    var a = on || nearestToMiddle();
    var ax = a ? a.x * sc + tx : 0, ay = a ? a.y * sc + ty : 0;
    change();
    if (!a) return;
    var m = a;
    while (m && !m.shown) m = m.pid ? by[m.pid] : null;
    if (!m) return;
    tx = ax - m.x * sc;
    ty = ay - m.y * sc;
    if (m !== a) clampX();
    apply();
  }

  // Unfold whatever is hiding a node, so "next open" can reach one that was
  // folded away rather than appearing to do nothing.
  function reveal(n) {
    var p = n.pid ? by[n.pid] : null;
    var moved = false;
    while (p) {
      if (p.folded) { fold(p, false); moved = true; }
      p = p.pid ? by[p.pid] : null;
    }
    if (moved) layout();
  }

  function flash(n) {
    n.el.classList.remove('flash');
    void n.el.offsetWidth;
    n.el.classList.add('flash');
  }

  // -- wiring --------------------------------------------------------------
  view.addEventListener('wheel', function (ev) {
    // The picker is inside .gview, so every wheel turn over its list of
    // recipes arrives here first.  Preventing it below is what moves the
    // canvas, and it is also what stops the list scrolling: over the list,
    // stand aside and let the browser scroll .gpop-body the ordinary way.
    // Over the rest of the panel there is nothing to scroll, so the turn is
    // swallowed rather than passed on -- panning the drawing out from under a
    // panel that is pinned to one of its boxes reads as a glitch, and the page
    // behind must not creep either.  .gpop-body contains that same creep at
    // the ends of the list; see `overscroll-behavior` in app.css.
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

  // A press on a box must pan the drawing, not hand the box to the operating
  // system.  Two things inside a drawing are native drag sources whether
  // anyone asked for them or not: an <img>, which is every box's icon, and
  // any text the browser has selected.  Once one of them takes the gesture
  // the browser owns the pointer -- pointermove stops arriving, the pan
  // freezes where it stood, and the cursor turns into the no-drop sign until
  // the button comes up.  Panning steadily one way is what finds them: the
  // drawing slides under a pointer that is standing still, so sooner or later
  // an icon arrives underneath it and every drag from that spot dies.  The <a>
  // already carries draggable="false" and that attribute says nothing about
  // the <img> inside it, so refuse the gesture for the whole drawing here
  // instead.  The picker is outside .gworld and keeps ordinary behaviour.
  world.addEventListener('dragstart', function (ev) { ev.preventDefault(); });

  // The canvas drags from anywhere, boxes included, so a drag that happens to
  // start on a box must not also open it.  The test is where the click landed
  // against where the pointer went down -- a distance, not a flag set during
  // the move: a few stray pixels of pointer jitter between down and up would
  // otherwise eat a click nobody meant as a drag.
  var drag = null, down = null;
  var SLOP = 5;

  view.addEventListener('pointerdown', function (ev) {
    // Cleared before the guards, not after: a press that this handler ignores
    // still ends whatever the last one started, so a gesture that never
    // produced a click cannot leave a stale origin behind for the next one to
    // measure itself against and be thrown away as a drag.
    down = null;
    drag = null;
    if (ev.button !== 0) return;
    if (ev.target.closest('button, select, input, .gpop')) return;
    down = { x: ev.clientX, y: ev.clientY };
    drag = { x: ev.clientX, y: ev.clientY, tx: tx, ty: ty, held: false };
  });

  // Capture is taken when the gesture turns out to be a drag, and not one
  // moment earlier.  Capturing on pointerdown looks harmless and is not: once
  // it is active the browser re-targets the pointerup to the capturing
  // element, the click is then dispatched at the nearest common ancestor --
  // .gview -- and a click on a box never reaches the box.  A real mouse always
  // moves a pixel or two between press and release, so that is every click a
  // person makes; only a synthetic click with no movement in between still
  // works, which is exactly the kind of bug a test does not see.  Listening on
  // the document rather than on .gview means a fast flick that leaves the
  // canvas before the threshold is met still starts the pan.
  document.addEventListener('pointermove', function (ev) {
    if (!drag) return;
    var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!drag.held) {
      if (Math.abs(dx) + Math.abs(dy) <= SLOP) return;
      drag.held = true;
      view.setPointerCapture(ev.pointerId);
      view.classList.add('dragging');
    }
    tx = drag.tx + dx;
    ty = drag.ty + dy;
    apply();
  });

  function endDrag() {
    drag = null;
    view.classList.remove('dragging');
  }
  // On the document: without capture -- which is now only taken once the
  // gesture is really a drag -- a release just outside the canvas would never
  // reach .gview, and the pan would carry on with no button held.
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
    // Dismiss the picker on a click that landed outside it -- here, and not on
    // pointerdown, so dragging the canvas to see more of the route keeps the
    // panel instead of throwing it away at the first pixel of the drag.
    if (pop && !pop.hidden && !ev.target.closest('.gpop, .gnode')) closePop();
  }, true);

  // A box is placed by transform, so its layout position is (0,0) and any
  // native scroll of the viewport slides the whole drawing out from under the
  // camera.  Two things do that: the browser's jump to #<node id> on load, and
  // tabbing to a link inside a box that is off screen.  Pin the container and
  // pan to the box instead, which is what was wanted in both cases.
  function onscreen(n) {
    var r = view.getBoundingClientRect();
    var x = n.x * sc + tx, y = n.y * sc + ty;
    return x >= 0 && y >= 0 &&
           x + NODE_W * sc <= r.width && y + NODE_H * sc <= r.height;
  }
  view.addEventListener('scroll', function () {
    view.scrollTop = 0;
    view.scrollLeft = 0;
  });
  world.addEventListener('focusin', function (ev) {
    var box = ev.target.closest('.gnode');
    var n = box && by[box.dataset.id];
    if (n && !onscreen(n)) centre(n);
  });

  // -- the in-place recipe picker ------------------------------------------
  // Clicking a box opens the choice list over the drawing instead of leaving
  // it.  The panel is a server-rendered fragment -- the same `offer` the node
  // page runs -- and the forms inside it post normally, so choosing is the
  // ordinary decision -> re-solve -> re-render loop with the fetch only
  // standing in for the trip out and back.
  pop = document.getElementById('gpop');
  var pid = view.dataset.pid;
  var seq = 0;

  function closePop() {
    pop.hidden = true;
    pop.innerHTML = '';
    if (picking) picking.el.classList.remove('picking');
    picking = null;
  }

  // Which side of the box the panel sits on.  Decided when it opens and then
  // kept: recomputing it every frame of a pan makes the panel jump its own
  // width the moment the box crosses the threshold.
  var popSide = 1;

  placePop = function (n, decide) {
    var r = view.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var right = n.x * sc + tx + NODE_W * sc + 12;
    var left = n.x * sc + tx - w - 12;
    if (decide) popSide = (right + w > r.width - 8 && left >= 8) ? -1 : 1;
    var x = popSide > 0 ? right : left;
    var y = n.y * sc + ty + NODE_H * sc / 2 - h / 2;
    // Clamped inside the viewport regardless: a panel half off the canvas is
    // worse than one that has moved.
    pop.style.left = Math.max(8, Math.min(x, r.width - w - 8)) + 'px';
    pop.style.top = Math.max(8, Math.min(y, r.height - h - 8)) + 'px';
  };

  function openPop(n, params) {
    var mine = ++seq;
    if (picking && picking !== n) picking.el.classList.remove('picking');
    picking = n;
    n.el.classList.add('picking');
    var url = '/plans/' + pid + '/node/' + n.id + '/offer';
    if (params) url += '?' + params;
    if (pop.hidden) {
      pop.innerHTML = '<div class="gpop-wait muted small">loading the recipes\u2026</div>';
      pop.hidden = false;
      placePop(n, true);
    }
    fetch(url)
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (mine !== seq) return;   // a later click won
        pop.innerHTML = html;
        placePop(n, true);   // the real height is only known now
        var q = pop.querySelector('.gpop-q');
        if (q) {
          q.focus();
          q.setSelectionRange(q.value.length, q.value.length);
        }
      })
      .catch(function () {
        if (mine === seq) {
          pop.innerHTML = '<div class="gpop-wait bad small">could not load the recipes' +
                          ' \u2014 open the item to choose one</div>';
        }
      });
  }

  // Refetch with different filters.  The panel re-renders itself the same way
  // it was built, so there is one code path and not two.
  function refilter(over) {
    if (!picking) return;
    var cur = pop.querySelector('.tabs a.on');
    var qbox = pop.querySelector('.gpop-q');
    var map = cur ? (cur.dataset.map || '') : '';
    var q = qbox ? qbox.value.trim() : '';
    if (over && over.map !== undefined) map = over.map;
    if (over && over.q !== undefined) q = over.q;
    var params = [];
    if (map) params.push('map=' + encodeURIComponent(map));
    if (q) params.push('q=' + encodeURIComponent(q));
    openPop(picking, params.join('&'));
  }

  pop.addEventListener('click', function (ev) {
    if (ev.target.closest('.gpop-close')) { closePop(); return; }
    var tab = ev.target.closest('[data-map]');
    if (tab) {
      ev.preventDefault();
      refilter({ map: tab.dataset.map,
                 q: tab.dataset.q !== undefined ? tab.dataset.q : undefined });
      return;
    }
    var clear = ev.target.closest('.gpop-filter');
    if (clear) { ev.preventDefault(); refilter({ q: clear.dataset.q || '' }); }
  });
  pop.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter') return;
    if (!ev.target.classList.contains('gpop-q')) return;
    ev.preventDefault();          // it is not in a form; do not submit anything
    refilter();
  });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !pop.hidden) closePop();
  });

  world.addEventListener('click', function (ev) {
    var hit = ev.target.closest('.ghit');
    if (hit) {
      // The link is real -- it is the no-JS path and what a middle click or
      // ctrl+click should still follow -- so only a plain click is intercepted.
      if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      var n = by[hit.closest('.gnode').dataset.id];
      if (picking === n && !pop.hidden) { closePop(); return; }
      if (!onscreen(n)) centre(n);
      openPop(n);
      return;
    }
    var tog = ev.target.closest('.gtog');
    if (!tog) return;
    ev.preventDefault();
    var n = by[tog.closest('.gnode').dataset.id];
    // Anchored on the box you clicked, because folding moves that one too: a
    // folded parent drops to a row of its own instead of sitting between
    // children that are no longer there, and a button that jumps out from
    // under the pointer is hard to press twice.
    keepSpot(function () { fold(n, !n.folded); layout(); }, n);
    if (picking) {
      // Folding can hide the very node the panel belongs to.
      if (picking.shown) { placePop(picking); } else { closePop(); }
    }
  });

  // Hovering lights the whole chain back to the root: on a ten-deep tree that
  // is the fastest way to answer "what is this for".
  var lit = [];
  function unlight() {
    lit.forEach(function (m) { m.el.classList.remove('lit'); });
    lit = [];
    svg.querySelectorAll('path.lit').forEach(function (p) {
      p.classList.remove('lit');
    });
  }
  world.addEventListener('mouseover', function (ev) {
    var box = ev.target.closest('.gnode');
    if (!box) return;
    var n = by[box.dataset.id];
    if (lit.length && lit[0] === n) return;
    unlight();
    var chain = {};
    var m = n;
    while (m) {
      m.el.classList.add('lit');
      lit.push(m);
      chain[m.id] = 1;
      m = m.pid ? by[m.pid] : null;
    }
    svg.querySelectorAll('path').forEach(function (path) {
      if (chain[path.getAttribute('data-to')]) path.classList.add('lit');
    });
  });
  view.addEventListener('mouseleave', unlight);

  document.getElementById('gin').addEventListener('click', function () { zoom(1.25); });
  document.getElementById('gout').addEventListener('click', function () { zoom(0.8); });
  document.getElementById('gfit').addEventListener('click', fit);
  document.getElementById('gexpand').addEventListener('click', function () {
    setDepth(-1);
  });
  depthSel.addEventListener('change', function () {
    setDepth(parseInt(this.value, 10));
  });

  var nextBtn = document.getElementById('gnext');
  if (nextBtn) {
    var opens = order.filter(function (n) { return n.kind === 'open'; });
    var at = -1;
    nextBtn.addEventListener('click', function () {
      if (!opens.length) return;
      at = (at + 1) % opens.length;
      var n = opens[at];
      reveal(n);
      centre(n);
      flash(n);
    });
  }

  function openView() {
    // Your depth if you have picked one in this tab, otherwise the deepest
    // level that still fits in a couple of screens.  Not written back: a
    // computed default is a guess about the window, not a choice, and should
    // be free to change when the window or the route does.
    var d0 = savedDepth();
    if (d0 === null) d0 = openingDepth(view.getBoundingClientRect().height * 2);
    depthSel.value = String(d0);
    foldBelow(d0);
    home();

    // Arriving from a decision taken here: the redirect carries #<node id>, so
    // land on the node you just changed rather than back at the root, and
    // unfold whatever the opening depth had hidden it behind.
    var want = location.hash.slice(1);
    if (want && by[want]) {
      reveal(by[want]);
      centre(by[want]);
      flash(by[want]);
    }
    view.scrollTop = 0;
    view.scrollLeft = 0;
    // Hidden until the camera is set, so nobody sees the unfolded first pass.
    world.classList.remove('pending');
  }

  // Twice, on purpose.  How much fits depends on the viewport's height, and at
  // end-of-body that is styled but not necessarily settled -- a height read too
  // early opens the view somewhere arbitrary.  The eager call means the drawing
  // is there even if the frame callback is throttled; the second corrects it.
  openView();
  requestAnimationFrame(openView);
})();
