// Applies the saved light/dark choice before the page paints (kept out of index.html so the
// Content-Security-Policy can forbid inline scripts).
try {
  const t = localStorage.getItem("wabbadrift-theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) { /* storage unavailable */ }
