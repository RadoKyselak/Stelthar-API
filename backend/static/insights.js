/* Page-view counts (Vercel Web Analytics: no cookies). The address is sent
   without its query or hash: a claim in a link (?claim=...) is never recorded. */
window.va = window.va || function () { (window.vaq = window.vaq || []).push(arguments); };
window.va("beforeSend", function (event) {
  try {
    const url = new URL(event.url);
    url.search = "";
    url.hash = "";
    return Object.assign({}, event, { url: url.toString() });
  } catch (e) { return null; }
});
