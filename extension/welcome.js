// The page opened once, on install: pin it, check one claim, where else it works.
// Each step is ticked as it really happens (pinned, a first check saved).
const HISTORY_KEY = "stelthar_history";
const PENDING_KEY = "mirador_pending";

document.addEventListener("DOMContentLoaded", () => {
  const $ = (id) => document.getElementById(id);

  // The shortcut the person actually has (they may have changed it).
  chrome.commands.getAll((cmds) => {
    const c = (cmds || []).find((x) => x.name === "open-panel");
    if (c && c.shortcut) $("shortcut").textContent = c.shortcut;
  });

  // Pinned? Chrome reports it; it can't be done for the person.
  const checkPinned = () => {
    if (!chrome.action.getUserSettings) return;
    chrome.action.getUserSettings().then((s) => {
      const on = !!(s && s.isOnToolbar);
      $("step-pin").classList.toggle("done", on);
      $("pin-status").textContent = on ? "Pinned." : "";
    }).catch(() => {});
  };
  checkPinned();
  setInterval(checkPinned, 1500);

  // A first check is done when one is saved.
  const checkTried = () => {
    chrome.storage.local.get([HISTORY_KEY], (d) => {
      const on = (d[HISTORY_KEY] || []).length > 0;
      $("step-try").classList.toggle("done", on);
      $("try-status").textContent = on ? "Done. The answer is in the side panel, with its source." : "";
    });
  };
  checkTried();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[HISTORY_KEY]) checkTried();
  });

  // One click: open the side panel and check the sample. The window id is
  // known before the click, so open() runs inside it (Chrome needs that).
  let windowId = null;
  chrome.windows.getCurrent().then((w) => { windowId = w.id; }).catch(() => {});
  $("try").addEventListener("click", () => {
    if (windowId == null) return;
    chrome.sidePanel.open({ windowId }).catch(() => {});
    chrome.storage.session.set({ [PENDING_KEY]: { text: $("sample").textContent, intent: "check", at: Date.now() } });
  });
});
