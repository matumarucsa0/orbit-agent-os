/* Orbit: small progressive enhancements. Every page works without this file. */
(function () {
  "use strict";

  var STORE_KEY = "orbit:last-form";

  function store(fn) {
    try { return fn(window.sessionStorage); } catch (e) { return null; }
  }

  // --- Toasts: dismiss button, info messages fade on their own; errors stay until dismissed. ---
  function dismiss(flash) {
    flash.classList.add("is-leaving");
    setTimeout(function () { flash.remove(); }, 200);
  }

  document.querySelectorAll(".flash").forEach(function (flash) {
    flash.querySelector(".flash-x").addEventListener("click", function () { dismiss(flash); });
    if (!flash.classList.contains("flash-error")) {
      setTimeout(function () { dismiss(flash); }, flash.classList.contains("has-action") ? 12000 : 5000);
    }
  });

  // --- A link or redirect to #section opens that section if it's a collapsed <details>. ---
  function openTarget() {
    var id = decodeURIComponent(location.hash.slice(1));
    var target = id && document.getElementById(id);
    if (!target) return;
    for (var node = target; node; node = node.parentElement) {
      if (node.tagName === "DETAILS") node.open = true;
    }
    target.scrollIntoView();
  }
  openTarget();
  window.addEventListener("hashchange", openTarget);

  // --- One submit per tap, and remember what was typed in case the server says no. ---
  function formKey(form) {
    return Array.prototype.indexOf.call(document.forms, form) + " " + (form.getAttribute("action") || "");
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented) return; // e.g. a cancelled confirm()
    if (form.dataset.submitting) {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "1";
    form.classList.add("is-submitting");
    // A slow submit (Claude is thinking) says so on its button; pageshow puts the label back.
    var busy = form.querySelector("[data-busy]");
    if (busy) {
      busy.dataset.label = busy.textContent;
      busy.textContent = busy.dataset.busy;
    }
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    var values = [];
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.type === "hidden" || el.type === "file" || el.type === "password") return;
      if (el.type === "checkbox" || el.type === "radio") {
        values.push([el.name, el.value, el.checked]);
      } else if (el.tagName === "SELECT" && el.multiple) {
        values.push([el.name, Array.prototype.filter.call(el.options, function (o) { return o.selected; })
          .map(function (o) { return o.value; })]);
      } else if (el.tagName !== "BUTTON") {
        values.push([el.name, el.value]);
      }
    });
    store(function (s) {
      s.setItem(STORE_KEY, JSON.stringify({ path: location.pathname, key: formKey(form), values: values }));
    });
  });

  // Back/forward cache: a page restored from history must accept a submit again.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-submitting]").forEach(function (form) {
      delete form.dataset.submitting;
      form.classList.remove("is-submitting");
      var busy = form.querySelector("[data-busy]");
      if (busy && busy.dataset.label) busy.textContent = busy.dataset.label;
    });
  });

  // After a rejected submit (an error toast on the same page), put the typed values back.
  var saved = store(function (s) {
    var raw = s.getItem(STORE_KEY);
    s.removeItem(STORE_KEY);
    return raw && JSON.parse(raw);
  });
  if (saved && saved.path === location.pathname && document.querySelector(".flash-error")) {
    var form = Array.prototype.find.call(document.forms, function (f) { return formKey(f) === saved.key; });
    if (form) {
      saved.values.forEach(function (entry) {
        var name = entry[0], value = entry[1];
        Array.prototype.forEach.call(form.elements, function (el) {
          if (el.name !== name) return;
          if (el.type === "checkbox" || el.type === "radio") {
            if (el.value === value) el.checked = entry[2];
          } else if (el.tagName === "SELECT" && el.multiple) {
            Array.prototype.forEach.call(el.options, function (o) { o.selected = value.indexOf(o.value) !== -1; });
          } else if (el.type !== "hidden") {
            el.value = value;
          }
        });
      });
      for (var node = form.parentElement; node; node = node.parentElement) {
        if (node.tagName === "DETAILS") node.open = true;
      }
      form.scrollIntoView({ block: "center" });
    }
  }

  // --- More menu: a click anywhere else, or Escape, closes the dropdown. ---
  var moreMenu = document.querySelector(".more-menu");
  if (moreMenu) {
    document.addEventListener("click", function (event) {
      if (!moreMenu.contains(event.target)) moreMenu.open = false;
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") moreMenu.open = false;
    });
  }

  // --- Contact sync review: "Select all" toggles every tick box in its section. ---
  document.querySelectorAll("[data-select-all]").forEach(function (button) {
    button.addEventListener("click", function () {
      var boxes = document.querySelectorAll('input[type="checkbox"][data-group="' + button.dataset.selectAll + '"]');
      var all = Array.prototype.every.call(boxes, function (box) { return box.checked; });
      boxes.forEach(function (box) { box.checked = !all; });
    });
  });
})();
