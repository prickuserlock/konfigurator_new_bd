(function () {
  const SCROLL_KEY = "scrollY:" + location.pathname;
  const DETAILS_PREFIX = "detailsOpen:";

  function saveScroll() {
    try { sessionStorage.setItem(SCROLL_KEY, String(window.scrollY || 0)); } catch (e) {}
  }

  function restoreScroll() {
    try {
      // если есть якорь — пусть браузер сам к нему прыгнет
      if (location.hash && location.hash.length > 1) return;

      const raw = sessionStorage.getItem(SCROLL_KEY);
      if (!raw) return;
      const y = parseInt(raw, 10);
      if (!Number.isFinite(y)) return;

      requestAnimationFrame(() => window.scrollTo(0, y));
    } catch (e) {}
  }

  function initScrollHooks() {
    // submit форм
    document.addEventListener("submit", (e) => {
      saveScroll();

      // если в форме есть return_to и оно пустое — заполним текущим URL (включая #якорь)
      const form = e.target;
      if (form && form instanceof HTMLFormElement) {
        const inp = form.querySelector('input[name="return_to"]');
        if (inp && !inp.value) {
          inp.value = location.pathname + location.search + location.hash;
        }
      }
    }, true);

    // клики по ссылкам (важно: уход с dashboard на edit_* тоже сохранит скролл)
    document.addEventListener("click", (e) => {
      const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a) return;
      if (a.target === "_blank") return;

      const href = a.getAttribute("href") || "";
      if (!href || href.startsWith("#")) return;

      saveScroll();
    }, true);

    window.addEventListener("beforeunload", saveScroll, { capture: true });
    document.addEventListener("DOMContentLoaded", restoreScroll);
  }

  function initDetailsMemory() {
    const list = document.querySelectorAll('details[data-remember="1"], details[data-remember]');
    list.forEach((d) => {
      const keyAttr = d.getAttribute("data-key") || d.id;
      if (!keyAttr) return;
      const key = DETAILS_PREFIX + keyAttr;

      try {
        const saved = localStorage.getItem(key);
        if (saved === "0") d.open = false;
        if (saved === "1") d.open = true;

        d.addEventListener("toggle", () => {
          try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (e) {}
        });
      } catch (e) {}
    });
  }

  function openDetailsForHash() {
    if (!location.hash) return;
    const el = document.querySelector(location.hash);
    if (!el) return;
    const det = el.closest && el.closest("details");
    if (det) det.open = true;
  }

  document.addEventListener("DOMContentLoaded", () => {
    initDetailsMemory();
    openDetailsForHash();
  });

  initScrollHooks();
})();