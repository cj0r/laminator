// Show only the fields that belong to the chosen backup destination type. Without JavaScript every field stays visible.
document.querySelectorAll("form[data-dest]").forEach(function (form) {
  var sel = form.querySelector("[data-dest-type]");
  function sync() {
    form.querySelectorAll("[data-dest-for]").forEach(function (box) { box.hidden = box.getAttribute("data-dest-for") !== sel.value; });
  }
  sel.addEventListener("change", sync);
  sync();
});

// A select with data-goto loads another page when changed (the Template page: pick a saved template to load its values).
document.querySelectorAll("select[data-goto]").forEach(function (sel) {
  sel.addEventListener("change", function () {
    var url = sel.getAttribute("data-goto") + encodeURIComponent(sel.value);
    guardedLeave(function () { window.location.href = url; }, function () { sel.value = sel.getAttribute("data-was") || sel.value; });
  });
  sel.setAttribute("data-was", sel.value);
});

// (i) icons that share a group are radio buttons (one open at a time); clicking the open one closes it, and Escape closes any.
document.addEventListener("click", function (e) {
  var icon = e.target.closest && e.target.closest("label.infoicon");
  if (!icon) return;
  var box = document.getElementById(icon.getAttribute("for"));
  if (box && box.type === "radio" && box.checked) { e.preventDefault(); box.checked = false; }
});
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".infotoggle:checked").forEach(function (b) { b.checked = false; });
});

// the menu button (narrow screens): Escape or a tap outside the header closes the open menu
(function () {
  var tog = document.getElementById("menu-toggle");
  if (!tog) return;
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && tog.checked) { tog.checked = false; } });
  document.addEventListener("click", function (e) { if (tog.checked && !(e.target.closest && e.target.closest("header"))) { tog.checked = false; } });
})();

// a select with data-autosubmit sends its form when changed (the preview template on an item's page)
document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
  sel.setAttribute("data-was", sel.value);
  sel.addEventListener("change", function () {
    guardedLeave(function () { sel.form.submit(); }, function () { sel.value = sel.getAttribute("data-was"); });
  });
});

// ---- unsaved changes ----------------------------------------------------------------------------------------------
// Forms you type into (options, server addresses, templates, overrides) remember how they looked on load. Leaving the page with
// edits that were not saved shows Discard changes / Continue editing; closing the tab or reloading gets the browser's own prompt.
var GUARDED = "form.optform, form.serverform, form.editform, form.pwform, form#ctl";
var leaving = false;

function fieldPairs(form) {
  var out = [];
  new FormData(form).forEach(function (v, k) {
    var el = form.elements.namedItem(k);  // not elements[k]: a field called "item" would give the item() method
    var type = el && (el.type || (el.length && el[0] && el[0].type));
    if (type === "hidden" || k === "token" || typeof v !== "string") return;
    out.push(k + "=" + v);
  });
  return out.join("&");
}
var guarded = Array.prototype.map.call(document.querySelectorAll(GUARDED), function (form) {
  return { form: form, start: fieldPairs(form) };
});
function unsaved() {
  return !leaving && guarded.some(function (g) { return fieldPairs(g.form) !== g.start; });
}
guarded.forEach(function (g) {
  g.form.addEventListener("submit", function () { leaving = true; });
});
if (guarded.length) {  // only pages with something to lose: a beforeunload handler would stop the browser keeping the page for an instant Back
  window.addEventListener("beforeunload", function (e) {
    if (unsaved()) { e.preventDefault(); e.returnValue = ""; }
  });
}
// the preview refresh on the Template page is not a save: keep the guard if a request fails
document.body.addEventListener("htmx:afterRequest", function () { leaving = false; });

function askLeave(onDiscard, onStay) {
  var box = document.createElement("div");
  box.className = "leavebox";
  box.innerHTML = '<div class="leavecard" role="alertdialog" aria-modal="true" aria-labelledby="leave-h" aria-describedby="leave-p">' +
    '<h2 id="leave-h">You have unsaved changes</h2><p id="leave-p">What you changed on this page has not been saved yet. If you leave now it is lost.</p>' +
    '<div class="row"><button type="button" class="btn danger" data-act="discard">Discard changes</button>' +
    '<button type="button" class="btn" data-act="stay">Continue editing</button></div></div>';
  var before = document.activeElement;
  function close(fn) { document.removeEventListener("keydown", onKey, true); box.remove(); if (before && before.focus) before.focus(); if (fn) fn(); }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); close(onStay); }
    if (e.key === "Tab") {
      var b = box.querySelectorAll("button"); var first = b[0], last = b[b.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }
  box.addEventListener("click", function (e) {
    var act = e.target.getAttribute && e.target.getAttribute("data-act");
    if (act === "discard") close(function () { leaving = true; onDiscard(); });
    else if (act === "stay" || e.target === box) close(onStay);
  });
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(box);
  box.querySelector('[data-act="stay"]').focus();
}
function guardedLeave(go, stay) {
  if (unsaved()) askLeave(go, stay || function () {}); else go();
}
document.addEventListener("click", function (e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  var a = e.target.closest && e.target.closest("a[href]");
  if (!a || a.target === "_blank" || a.hasAttribute("download")) return;
  var href = a.getAttribute("href");
  if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
  if (!unsaved()) return;
  e.preventDefault();
  askLeave(function () { window.location.href = a.href; }, function () {});
}, true);

// ---- keep what was typed when a save is refused ---------------------------------------------------------------------------
// A form is remembered (this tab only, never passwords or files) when sent. If the page that comes back shows an error, the values
// are put back so nothing has to be typed twice; after a successful save they are dropped.
var KEEP = "laminator-typed:";
// true when the page in front of you is the answer to a save that was refused (nothing stored), not one that went through
function refused() {
  var nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
  return !!document.querySelector(".note.bad") && ((nav && nav.responseStatus === 422) || /[?&]note=bad(&|$)/.test(location.search));
}
function typedValues(form) {
  var vals = {};
  Array.prototype.forEach.call(form.elements, function (el) {
    if (!el.name || el.type === "hidden" || el.type === "password" || el.type === "file" || el.type === "submit" || el.type === "button") return;
    vals[el.name] = (el.type === "checkbox" || el.type === "radio") ? (el.checked ? "1:" + el.value : "0:" + el.value) : el.value;
  });
  return vals;
}
document.querySelectorAll(GUARDED).forEach(function (form) {
  var key = KEEP + location.pathname + "|" + (form.getAttribute("action") || "");
  var saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(key) || "null"); } catch (err) { saved = null; }
  try { sessionStorage.removeItem(key); } catch (err) { /* private mode: nothing to keep */ }
  if (saved && refused()) {
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || !(el.name in saved) || el.type === "hidden" || el.type === "password" || el.type === "file") return;
      var v = saved[el.name];
      if (el.type === "checkbox" || el.type === "radio") { if (v.indexOf(":" + el.value) > 0) el.checked = v.charAt(0) === "1"; }
      else if (el.value !== v) el.value = v;
    });
  }
  form.addEventListener("submit", function () {
    try { sessionStorage.setItem(KEEP + location.pathname + "|" + (form.getAttribute("action") || ""), JSON.stringify(typedValues(form))); } catch (err) { /* ignore */ }
  });
});

// ---- the Library keeps loading before you get to the end ---------------------------------------------------------------------
// The "loading more" marker at the end of the list asks for the next batch while it is still about two screens away, so the new
// titles are there by the time you arrive. After each batch the address records how many batches are shown (page_no), so Back
// from a title rebuilds the same list and the browser puts you back where you were.
if ("IntersectionObserver" in window && window.htmx) {
  var more = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { more.unobserve(e.target); window.htmx.trigger(e.target, "loadmore"); }
    });
  }, { rootMargin: "0px 0px 1800px 0px" });
  var watchMore = function () {
    document.querySelectorAll(".sentinel:not([data-watched])").forEach(function (el) { el.setAttribute("data-watched", "1"); more.observe(el); });
  };
  watchMore();
  document.body.addEventListener("htmx:afterSettle", watchMore);
  document.body.addEventListener("htmx:beforeSwap", function (e) {  // fires while the marker is still in the page (afterRequest comes from a detached element)
    var el = e.detail && e.detail.elt;
    var n = el && el.classList && el.classList.contains("sentinel") && e.detail.xhr && e.detail.xhr.status === 200 ? el.getAttribute("data-chunk") : null;
    if (!n || !window.history.replaceState) return;
    var url = new URL(window.location.href);
    url.searchParams.set("page_no", n);
    window.history.replaceState(window.history.state, "", url.toString());
  });
}

// ---- Metadata overrides: search and filter the planned changes as you type (nothing is sent to the server) -----------------------------
(function () {
  var q = document.getElementById("pf-q"), st = document.getElementById("pf-status"), fl = document.getElementById("pf-field");
  if (!q || !st || !fl) return;
  var cards = Array.prototype.slice.call(document.querySelectorAll("section.plan[data-search]"));
  var count = document.getElementById("pf-count"), none = document.getElementById("pf-none"), reset = document.getElementById("pf-reset");
  function words(s) { return s.split(/\s+/).filter(Boolean); }
  function apply() {
    var terms = words(q.value.toLowerCase()), s = st.value, f = fl.value, shown = 0;
    cards.forEach(function (c) {
      var ok = terms.every(function (t) { return c.getAttribute("data-search").indexOf(t) !== -1; })
        && (!s || words(c.getAttribute("data-status")).indexOf(s) !== -1)
        && (!f || words(c.getAttribute("data-fields")).indexOf(f) !== -1);
      c.hidden = !ok;
      if (ok) shown++;
    });
    count.textContent = (q.value || s || f) ? "showing " + shown + " of " + cards.length : "";
    none.hidden = shown !== 0;
    if (reset) reset.hidden = !(q.value || s || f);
  }
  if (reset) reset.addEventListener("click", function () { q.value = ""; st.value = ""; fl.value = ""; apply(); q.focus(); });
  ["input", "change"].forEach(function (ev) { q.addEventListener(ev, apply); st.addEventListener(ev, apply); fl.addEventListener(ev, apply); });
  apply();
})();

// A note that belongs to a form (a refused save, a result) sits next to that form; bring it into view, however the page was reached.
(function () {
  var n = document.querySelector(".note[data-focus]");
  if (!n) { return; }
  var go = function () { n.scrollIntoView({ block: "center" }); try { n.focus(); } catch (err) { /* ignore */ } };
  go();
  window.addEventListener("load", go);  // again once images have set the page's height
  window.setTimeout(go, 300);           // and once more after the first paint, in case the browser restored a scroll position meanwhile
})();
