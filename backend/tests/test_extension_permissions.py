"""The extension asks for as little as it can, so it installs with no
permission warning and passes Chrome Web Store review quickly.

  activeTab     the right-click item, shortcut and toolbar icon give access
                to the current tab only, only when the user uses them
  scripting     inject the card into that tab, read the highlighted text
  storage       saved results and recent checks (no warning)
  contextMenus  the "Check with Project Mirador" right-click item (no warning)

No content script runs on any page by itself, and the extension has no host
access: the API allows extension origins through CORS.
"""
import json
import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parents[2] / "extension"
MANIFEST = json.loads((EXT / "manifest.json").read_text())
CODE = {p.name: re.sub(r"/\*.*?\*/|//[^\n]*", "", p.read_text(), flags=re.S) for p in EXT.glob("*.js")}

# chrome.* namespaces that need a manifest permission. The rest (runtime,
# tabs for ids and urls it opens, action, commands, omnibox) need none.
_NEEDS = {"contextMenus": "contextMenus", "scripting": "scripting", "storage": "storage",
          "notifications": "notifications", "offscreen": "offscreen", "downloads": "downloads",
          "history": "history", "bookmarks": "bookmarks", "cookies": "cookies",
          "webRequest": "webRequest", "identity": "identity", "alarms": "alarms"}


def test_only_four_permissions():
    assert sorted(MANIFEST["permissions"]) == ["activeTab", "contextMenus", "scripting", "storage"]


def test_no_access_to_any_website():
    for key in ("host_permissions", "optional_host_permissions", "content_scripts",
                "web_accessible_resources", "externally_connectable"):
        assert key not in MANIFEST, key
    assert "<all_urls>" not in json.dumps(MANIFEST)


def test_every_api_used_is_declared_and_every_permission_is_used():
    used = set()
    for code in CODE.values():
        used |= set(re.findall(r"\bchrome\.([a-zA-Z]+)\.", code))
    needed = {_NEEDS[ns] for ns in used if ns in _NEEDS}
    declared = set(MANIFEST["permissions"]) - {"activeTab"}
    assert needed <= declared, needed - declared            # nothing used without asking
    assert declared <= needed, declared - needed            # nothing asked for and unused


def test_card_is_injected_only_on_request():
    bg = CODE["background.js"]
    assert 'files: ["result-view.js", "content.js"]' in bg
    # The card script no longer watches every selection on every page.
    assert "mouseup" not in CODE["content.js"]


def test_api_calls_work_without_host_access(test_client):
    """With no host permission the extension's requests are ordinary CORS
    requests from its own origin; the API must allow them."""
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    r = test_client.options("/v2/lookup", headers={
        "origin": origin, "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert r.headers.get("access-control-allow-origin") in (origin, "*")
