(() => {
  const KEY = "bd_theme"; // ключ в localStorage
  const root = document.documentElement;

  function getSystemTheme() {
    try {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    } catch {
      return "light";
    }
  }

  function applyTheme(theme) {
    const t = theme === "dark" ? "dark" : "light";
    root.setAttribute("data-theme", t);

    // обновляем кнопку, если она есть
    const btn = document.getElementById("themeToggle");
    if (btn) {
      const isDark = t === "dark";
      btn.setAttribute("aria-pressed", isDark ? "true" : "false");
      btn.setAttribute("title", isDark ? "Тёмная тема" : "Светлая тема");

      // рисуем иконку/текст внутри кнопки
      btn.innerHTML = isDark
        ? `<span class="theme-ico" aria-hidden="true">🌙</span><span class="theme-txt">Тёмная</span>`
        : `<span class="theme-ico" aria-hidden="true">☀️</span><span class="theme-txt">Светлая</span>`;
    }
  }

  function initTheme() {
    const saved = localStorage.getItem(KEY);
    const initial = saved || getSystemTheme();
    applyTheme(initial);
  }

  function toggleTheme() {
    const current = root.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = current === "dark" ? "light" : "dark";
    localStorage.setItem(KEY, next);
    applyTheme(next);
  }

  // применяем максимально рано (чтобы не мигало)
  initTheme();

  // привязываем кнопку
  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("themeToggle");
    // синхронизируем подпись/иконку кнопки после появления в DOM
    applyTheme(localStorage.getItem(KEY) || getSystemTheme());
    if (btn) btn.addEventListener("click", toggleTheme);

    // если тема меняется в другой вкладке
    window.addEventListener("storage", (e) => {
      if (e.key === KEY) applyTheme(e.newValue || getSystemTheme());
    });
  });

  // на всякий случай — чтобы можно было дернуть из консоли
  window.__toggleTheme = toggleTheme;
})();
