/* Headcount picker — tap little people to set the count.
   Progressive enhancement: the real <input type="number"> is the source of
   truth and the no-JS fallback; tappable person/child icons layer on top and
   write back to it, so /<slug>/rsvp still receives a plain int.

   The icon row is capped to whatever fits ONE line (measured from the box
   width, reserving room for the trailing control) — it never wraps to a second
   row. Tap a person to set the count (springy). The trailing pill shows "N+"
   and reveals more people up to the one-row limit; once full it becomes "…",
   which opens a type-a-number field for big gatherings. */
(function () {
  var BASE = 6, STEP = 6, HARD_MAX = 40;
  // Chunky, big-headed "villager" silhouettes (Animal Crossing-ish).
  var PERSON =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="7.4" r="5"/><path d="M4.5 21c0-4.4 3.4-7 7.5-7s7.5 2.6 7.5 7z"/></svg>';
  var CHILD =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="8.4" r="4.4"/><path d="M6.4 21c0-3.6 2.6-5.6 5.6-5.6s5.6 2 5.6 5.6z"/></svg>';

  var relayouts = [];

  function setup(box) {
    var input = box.querySelector(".count-fallback");
    if (!input) return;
    var isKid = box.classList.contains("kids");
    var max = parseInt(input.getAttribute("max") || "99", 10);
    var icons = box.querySelector(".counter-icons");
    var numEl = box.querySelector(".counter-num");
    var more = null, maxFit = HARD_MAX, capacity = 0;
    var val = clamp(parseInt(input.value || "0", 10) || 0);

    icons.appendChild(input); // the number field lives at the end of the row

    function clamp(n) { n = n || 0; return n < 0 ? 0 : n > max ? max : n; }
    function pop(el) { el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop"); }
    function seats() { return icons.querySelectorAll(".seat"); }

    function makeSeat(n) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "seat"; b.innerHTML = isKid ? CHILD : PERSON;
      b.addEventListener("click", function () {
        var prev = val;
        val = val === n ? n - 1 : n;            // tap the last filled one to drop by one
        input.classList.remove("shown");
        if (more) more.style.display = "";
        paint();
        if (val > prev) pop(b);
      });
      b.addEventListener("animationend", function () { b.classList.remove("reveal", "pop"); });
      return b;
    }

    function labelMore() { if (more) more.textContent = capacity < maxFit ? capacity + "+" : "…"; }

    function buildMore() {
      more = document.createElement("button");
      more.type = "button"; more.className = "counter-more";
      more.addEventListener("click", function () {
        if (capacity < maxFit) {
          val = capacity;                       // select all shown...
          var add = Math.min(STEP, maxFit - capacity);
          for (var i = capacity + 1; i <= capacity + add; i++) {  // ...reveal the next, still one row
            var s = makeSeat(i); s.classList.add("reveal");
            icons.insertBefore(s, more);
          }
          capacity += add;
          labelMore(); paint();
        } else {
          more.style.display = "none";          // row is full — switch to typing
          input.classList.add("shown");
          input.focus(); input.select();
        }
      });
      icons.insertBefore(more, input);
    }

    function computeFit() {
      var s = icons.querySelector(".seat");
      if (!s) return HARD_MAX;
      var cs = getComputedStyle(box);
      var avail = box.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 10;
      var gap = parseFloat(getComputedStyle(icons).gap) || 6;
      var per = s.getBoundingClientRect().width + gap;
      // Reserve room for the trailing control — the wider of the pill or the
      // number field — so revealing it never pushes onto a second row.
      var reserve = Math.max(more ? more.getBoundingClientRect().width : 44, 90) + gap;
      return Math.max(3, Math.min(HARD_MAX, Math.floor((avail - reserve) / per)));
    }

    function setSeatCount(n) {
      var s = seats(), cur = s.length;
      if (n > cur) for (var i = cur + 1; i <= n; i++) icons.insertBefore(makeSeat(i), more);
      else for (var k = cur - 1; k >= n; k--) s[k].remove();
      capacity = n;
    }

    function paint() {
      val = clamp(val);
      var s = seats();
      for (var j = 0; j < s.length; j++) s[j].setAttribute("aria-pressed", j + 1 <= val ? "true" : "false");
      if (numEl.textContent !== String(val)) {
        numEl.textContent = val;
        numEl.classList.remove("pop"); void numEl.offsetWidth; numEl.classList.add("pop");
      }
      input.value = val;
    }

    function relayout(initial) {
      maxFit = computeFit();
      var want = initial
        ? Math.min(maxFit, Math.max(BASE, val <= maxFit ? val : maxFit))
        : Math.min(capacity, maxFit);           // on resize, only shrink
      setSeatCount(want);
      if (val > maxFit) { more.style.display = "none"; input.classList.add("shown"); }
      else { more.style.display = ""; input.classList.remove("shown"); }
      labelMore(); paint();
    }

    input.addEventListener("input", function () {
      val = clamp(parseInt(input.value || "0", 10) || 0);
      var s = seats();
      for (var j = 0; j < s.length; j++) s[j].setAttribute("aria-pressed", j + 1 <= val ? "true" : "false");
      numEl.textContent = val;
    });

    for (var i = 1; i <= BASE; i++) icons.insertBefore(makeSeat(i), input);
    buildMore();
    relayout(true);
    relayouts.push(relayout);
  }

  var t;
  window.addEventListener("resize", function () {
    clearTimeout(t);
    t = setTimeout(function () { for (var i = 0; i < relayouts.length; i++) relayouts[i](false); }, 150);
  });

  document.addEventListener("DOMContentLoaded", function () {
    var boxes = document.querySelectorAll("[data-counter]");
    for (var i = 0; i < boxes.length; i++) setup(boxes[i]);
  });
})();
