"""The extension asks for as little as it can, so it installs with no
permission warning and passes Chrome Web Store review quickly.

  sidePanel     results open in Chrome's side panel (no warning)
  contextMenus  the "Check with Project Mirador" right-click item, which also
                hands over the highlighted text (no warning)
  storage       saved results and recent checks (no warning)

Nothing runs on any web page, and the extension has no host access: the API
allows extension origins through CORS.
"""
import json
import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parents[2] / "extension"
MANIFEST = json.loads((EXT / "manifest.json").read_text())
CODE = {p.name: re.sub(r"/\*.*?\*/|//[^\n]*", "", p.read_text(), flags=re.S) for p in EXT.glob("*.js")}

# chrome.* namespaces that need a manifest permission. The rest (runtime,
# tabs.create, windows, action, commands, omnibox) need none.
_NEEDS = {"contextMenus": "contextMenus", "scripting": "scripting", "storage": "storage",
          "sidePanel": "sidePanel", "notifications": "notifications", "offscreen": "offscreen",
          "downloads": "downloads", "history": "history", "bookmarks": "bookmarks",
          "cookies": "cookies", "webRequest": "webRequest", "identity": "identity",
          "alarms": "alarms"}


def test_only_three_permissions():
    assert sorted(MANIFEST["permissions"]) == ["contextMenus", "sidePanel", "storage"]


def test_no_access_to_any_website():
    for key in ("host_permissions", "optional_host_permissions", "optional_permissions",
                "content_scripts", "web_accessible_resources", "externally_connectable"):
        assert key not in MANIFEST, key
    assert "<all_urls>" not in json.dumps(MANIFEST)
    assert "activeTab" not in MANIFEST["permissions"]


def test_every_api_used_is_declared_and_every_permission_is_used():
    used = set()
    for code in CODE.values():
        used |= set(re.findall(r"\bchrome\.([a-zA-Z]+)\.", code))
    needed = {_NEEDS[ns] for ns in used if ns in _NEEDS}
    declared = set(MANIFEST["permissions"])
    assert needed <= declared, needed - declared            # nothing used without asking
    assert declared <= needed, declared - needed            # nothing asked for and unused


def test_results_open_in_the_side_panel():
    assert MANIFEST["side_panel"]["default_path"] == "panel.html"
    assert "default_popup" not in MANIFEST["action"]
    bg = CODE["background.js"]
    assert "openPanelOnActionClick: true" in bg
    # The panel must open before any await, or Chrome drops the user's click.
    menu = bg[bg.index("contextMenus.onClicked"):]
    assert menu.index("sidePanel.open") < menu.index("sendToPanel(")


def test_address_bar_opens_the_side_panel_too():
    """Enter in the address bar ("mir ...") opens the result in the side
    panel, called before anything async so Chrome counts the keypress; a tab
    is only the fallback when Chrome refuses."""
    bg = CODE["background.js"]
    entered = bg[bg.index("omnibox.onInputEntered"):]
    entered = entered[:entered.index("\n});") + 4]
    assert "await" not in entered
    assert entered.index("sidePanel.open") < entered.index("sendToPanel(")
    assert ".catch(inTab)" in entered


def test_every_file_the_extension_loads_exists():
    page = (EXT / "panel.html").read_text()
    for src in re.findall(r'<script src="([^"]+)"', page):
        assert (EXT / src).is_file(), src
    assert (EXT / MANIFEST["background"]["service_worker"]).is_file()


def test_card_renderer_matches_the_website():
    web = EXT.parent / "backend" / "static" / "result-view.js"
    assert (EXT / "result-view.js").read_text() == web.read_text()


def test_api_calls_work_without_host_access(test_client):
    """With no host permission the extension's requests are ordinary CORS
    requests from its own origin; the API must allow them."""
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    r = test_client.options("/v2/lookup", headers={
        "origin": origin, "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert r.headers.get("access-control-allow-origin") in (origin, "*")
