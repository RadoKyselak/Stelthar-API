/*
 * Project Mirador: the extension's look, matching the website.
 *
 * Black and white with a yellow marker on the answer, a large figure, the
 * source rows in a soft panel and black pill buttons. It sits on top of the
 * shared card styles in result-view.js, which stays identical to the
 * website's copy, so only the extension's side panel uses it.
 */
(function (global) {
  "use strict";

  const CSS = `
.mv { --ink:#000; --ink2:#2b2b2b; --muted:#6b6b6b; --line:#e4e4df; --subtle:#f6f6f3; --mark:#ffe58a;
  --accent:#111; --accent-hover:#000; --on-accent:#fff;
  font: 500 14px/1.5 "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif; }
.mv .mv-alert { display: block; }
.mv [data-verdict="unavailable"], .mv [data-verdict="no-verdict"] { --tone: var(--muted); }
.mv .mv-alert-h { display: flex; width: max-content; margin: 0 0 10px; padding: 0; background: none;
  color: var(--tone); font-size: 12.5px; font-weight: 500; }
.mv .mv-alert-b { margin: 0; font-size: 15px; line-height: 1.55; color: var(--ink); }
.mv .mv-alert-b + .mv-alert-b { margin-top: 6px; }
/* The marker goes on answers only, not on errors or "can't verify". */
.mv .mv-alert:not([data-verdict="unavailable"]):not([data-verdict="no-verdict"]) .mv-alert-b {
  display: inline; padding: 1px 3px; font-size: 15.5px; color: #000; background: #ffe58a;
  -webkit-box-decoration-break: clone; box-decoration-break: clone; }
.mv .mv-sec { margin-top: 22px; }
.mv .mv-label { font-size: 13px; font-weight: 500; color: var(--ink); }
.mv .mv-fig { margin: 2px 0 0; font-size: 52px; font-weight: 500; letter-spacing: -.035em; line-height: 1; color: var(--ink); }
.mv .mv-fig.mv-long { font-size: 34px; }
.mv .mv-what { font-size: 12.5px; color: var(--muted); }
.mv .mv-ex { margin-top: 16px; border: 0; border-radius: 10px; background: var(--subtle); padding: 2px; }
.mv .mv-ex-h { background: none; border: 0; color: var(--ink); font-size: 12px; font-weight: 500; padding: 10px 12px 6px; }
.mv .mv-ex-t td { border-top: 1px solid var(--line); padding: 8px 12px; }
.mv .mv-ex-t tr.mv-cited td { background: #ffe58a; color: #000; }
.mv .mv-ex-n { border-top: 0; }
.mv .mv-foot { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--line); }
.mv .mv-src { font-size: 12.5px; color: var(--muted); }
.mv .mv-fc { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--line); }
.mv .mv-fc-list li { font-size: 13.5px; }
.mv a, .mv .mv-src a { color: var(--ink); text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 3px; }
.mv .mv-btn { border-radius: 999px; padding: 8px 15px; font-weight: 500; transition: opacity .12s; }
.mv .mv-btn:hover { background: var(--accent); opacity: .85; }
.mv .mv-btn-outline { background: transparent; color: var(--ink); box-shadow: inset 0 0 0 1px var(--line); }
.mv .mv-btn-outline:hover { background: var(--subtle); opacity: 1; }
.mv .mv-linkbtn { color: var(--ink); text-decoration: underline; text-decoration-thickness: 1px; text-underline-offset: 3px; }
.mv .mv-linkbtn:hover { text-decoration-thickness: 2px; }
.mv .mv-status { color: var(--ink); }
.mv .mv-loading { display: flex; align-items: center; gap: 10px; font-size: 13.5px; }
.mv .mv-loading::before { content: ""; flex: none; width: 14px; height: 14px; border-radius: 50%;
  border: 2px solid var(--line); border-top-color: var(--ink); animation: mv-spin .8s linear infinite; }
@keyframes mv-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mv .mv-loading::before { animation-duration: 2.4s; } }
`;

  global.MiradorSkin = Object.freeze({ css: CSS });
})(typeof window !== "undefined" ? window : globalThis);
