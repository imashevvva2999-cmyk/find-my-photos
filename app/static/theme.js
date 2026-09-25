// Theme switch (Auto / Dark / Light), applied on every page before it is drawn.
// "Auto" follows the device setting. The choice is stored only in this browser.
(() => {
  const KEY = "fmp-theme";
  const ORDER = ["auto", "dark", "light"];
  const LABELS = { auto: "Тема: авто", dark: "Тема: тёмная", light: "Тема: светлая" };
  const root = document.documentElement;

  const read = () => {
    try { return ORDER.includes(localStorage.getItem(KEY)) ? localStorage.getItem(KEY) : "auto"; }
    catch { return "auto"; }
  };
  const apply = (mode) => {
    if (mode === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", mode);
  };

  apply(read());
  // Keep other open tabs of the site in the same theme.
  window.addEventListener("storage", (e) => { if (e.key === KEY) apply(read()); });

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    let mode = read();
    button.textContent = LABELS[mode];
    button.addEventListener("click", () => {
      mode = ORDER[(ORDER.indexOf(mode) + 1) % ORDER.length];
      try { localStorage.setItem(KEY, mode); } catch { /* private window: works for this page only */ }
      apply(mode);
      button.textContent = LABELS[mode];
    });
  });
})();
