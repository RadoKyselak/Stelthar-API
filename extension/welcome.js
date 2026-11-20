// The page opened once, on install: practise the one move that matters,
// highlight a sentence and right-click it. Each step ticks as it happens.
const HISTORY_KEY = "stelthar_history";
const PENDING_KEY = "mirador_pending";

document.addEventListener("DOMContentLoaded", () => {
  const $ = (id) => document.getElementById(id);
  const sample = $("sample");

  // The shortcut the person actually has (they may have changed it).
  chrome.commands.getAll((cmds) => {
    const c = (cmds || []).find((x) => x.name === "open-panel");
    if (c && c.shortcut) $("shortcut").textContent = c.shortcut;
  });

  // Step 1: some of the sample sentence is selected.
  document.addEventListener("selectionchange", () => {
    const sel = document.getSelection();
    if (sel && !sel.isCollapsed && sel.toString().trim() && sel.containsNode(sample, true)) {
      $("step-highlight").classList.add("done");
    }
  });

  // Step 2: a check was saved (the right-click ran it).
  const checkDone = () => {
    chrome.storage.local.get([HISTORY_KEY], (d) => {
      if (!(d[HISTORY_KEY] || []).length) return;
      $("step-highlight").classList.add("done");
      $("step-check").classList.add("done");
      $("done").textContent = "That's it. The answer is in the side panel, with its source.";
      $("try").hidden = true;
    });
  };
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[HISTORY_KEY]) checkDone();
  });

  // If the menu doesn't appear: open the side panel and check the sample.
  // The window id is known before the click, so open() runs inside it.
  let windowId = null;
  chrome.windows.getCurrent().then((w) => { windowId = w.id; }).catch(() => {});
  $("try").addEventListener("click", () => {
    if (windowId == null) return;
    chrome.sidePanel.open({ windowId }).catch(() => {});
    chrome.storage.session.set({ [PENDING_KEY]: { text: sample.textContent, intent: "check", at: Date.now() } });
  });
});
