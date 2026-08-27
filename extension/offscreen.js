// A service worker has no DOM, so it cannot reach the clipboard. This offscreen
// document exists only to perform the write.
//
// It uses execCommand('copy') with a copy-event listener rather than
// navigator.clipboard.write, because the async Clipboard API requires document
// focus and an offscreen document never has it. The copy event is also the only
// way to put two flavours on the clipboard at once:
//
//   text/plain  ->  pasted into a code editor or a CMS field
//   text/html   ->  pasted into Google Docs or Word, where it arrives as a
//                   live hyperlink instead of a URL the writer has to relink
//
// That dual write is the whole ergonomic point. A citation that lands unlinked
// has to be rebuilt by hand, which is the work this feature exists to remove.
function writeBoth(plain, html) {
  const staging = document.getElementById("staging");

  const onCopy = (event) => {
    event.clipboardData.setData("text/plain", plain);
    if (html) event.clipboardData.setData("text/html", html);
    event.preventDefault();
  };

  document.addEventListener("copy", onCopy);
  try {
    // execCommand('copy') is a no-op without a selection, hence the textarea.
    staging.value = plain;
    staging.select();
    return document.execCommand("copy");
  } finally {
    document.removeEventListener("copy", onCopy);
    staging.value = "";
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.target !== "offscreen-clipboard") return false;
  let ok = false;
  try {
    ok = writeBoth(message.plain || "", message.html || "");
  } catch (e) {
    ok = false;
  }
  sendResponse({ ok });
  return false;
});
