// Small conveniences. Every page works without this file.
(() => {
  // First run: offer the time zone of the device the household is being set up on.
  const zone = document.querySelector("select[data-device-zone]");
  if (zone) {
    try {
      const mine = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if ([...zone.options].some(option => option.value === mine)) zone.value = mine;
    } catch { /* the server's default stays */ }
  }

  // Copy buttons stay hidden where the browser cannot copy (plain http that is not localhost).
  const show = root => { if (navigator.clipboard) root.querySelectorAll("[data-copy]").forEach(b => b.hidden = false); };
  show(document);
  document.addEventListener("htmx:afterSwap", event => show(event.target));
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    navigator.clipboard.writeText(button.dataset.copy).then(() => {
      const label = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = label; }, 1600);
    });
  });

  // A request that failed says so, rather than leaving the page as it was.
  let hide;
  const say = words => {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = words;
    clearTimeout(hide);
    hide = setTimeout(() => { toast.textContent = ""; }, 5000);
  };
  document.addEventListener("htmx:responseError", () => say("That didn't work. Reload the page and try again."));
  document.addEventListener("htmx:sendError", () => say("No connection. Check your internet and try again."));
})();
