"""Real browser, real server process, fake provider: the console works for a person."""
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import LOOP, PASSWORD, PASSWORD_HASH, drawing_png, ffmpeg_bin

playwright_sync = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = free_port()
    env = {**os.environ, "ADMIN_PASSWORD_HASH": PASSWORD_HASH, "LOOP_TOKEN": LOOP, "DATA_DIR": str(tmp_path / "data"),
           "FFMPEG_BIN": ffmpeg_bin(), "PROVIDER": "mock", "PORT": str(port), "HOST": "127.0.0.1",
           "PYTHONPATH": str(ROOT)}
    env.pop("ADMIN_TOKEN", None)
    proc = subprocess.Popen([sys.executable, "-m", "hallway"], cwd=tmp_path, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("server did not start: " + proc.stdout.read())
    yield base
    proc.terminate()
    proc.wait(timeout=10)


GEOMETRY = """() => {
  const out = [];
  for (const e of document.querySelectorAll('#rows button, #rows input, #rows video, #rows img, #rows .pill')) {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > window.innerWidth + 0.5 || r.left < -0.5) {
      out.push(e.tagName + ':' + ((e.textContent || e.getAttribute('aria-label') || '').trim()) + ' right=' + Math.round(r.right));
    }
  }
  const wrap = document.querySelector('.table-wrap');
  if (wrap.scrollWidth > wrap.clientWidth + 1) out.push('table scrolls sideways: ' + wrap.scrollWidth + ' > ' + wrap.clientWidth);
  if (document.documentElement.scrollWidth > window.innerWidth) out.push('page scrolls sideways');
  return out;
}"""


EXPECTED_401 = "Failed to load resource: the server responded with a status of 401 (UNAUTHORIZED)"


def watch(page, errors, allowed_401):
    """Collect console errors. `allowed_401` is how many 401 lines this page may log on purpose:
    a visit without a cookie asks /api/session and gets one; a wrong password gets one."""
    budget = [allowed_401]

    def on_console(m):
        if m.type != "error":
            return
        if m.text == EXPECTED_401 and budget[0] > 0:
            budget[0] -= 1
            return
        errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return budget


TOUCH = """() => {
  const small = [];
  for (const e of document.querySelectorAll('#app button, #app select, #app input[type=checkbox], #app .pill, dialog[open] button, dialog[open] textarea')) {
    const target = (e.tagName === 'INPUT' && e.closest('label')) ? e.closest('label') : e;
    const r = target.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.width < 43.5 || r.height < 43.5) {
      small.push(e.tagName + ':' + ((e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 20)) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
    }
  }
  return small;
}"""


def sign_in(page, server, errors):
    watch(page, errors, 1)
    page.goto(server + "/")
    page.wait_for_selector("#login:not([hidden])")
    page.fill("#password", PASSWORD)
    page.click("#login-form button")
    page.wait_for_selector("#app:not([hidden])")
    assert not page.is_visible("#login")


def add_drawing(page, png):
    page.click("#add-drawing")
    page.set_input_files("#upload-file", str(png))
    page.check("#upload-consent")
    page.click("#upload-form button[type=submit]")
    page.wait_for_selector("#rows tr")


def make_clip(page, button_name, prompt=None):
    page.get_by_role("button", name=button_name).first.click()
    page.wait_for_selector("#modal[open] #prompt-box")
    assert "costs nothing" in page.inner_text("#modal-body")
    if prompt is not None:
        page.fill("#prompt-box", prompt)
    page.get_by_role("button", name="Make clip (free)").click()


def test_person_makes_versions_and_publishes(server, tmp_path):
    shots = Path(os.environ.get("HH_SCREENSHOT_DIR", tmp_path))
    shots.mkdir(parents=True, exist_ok=True)
    png = tmp_path / "drawing.png"
    png.write_bytes(drawing_png())
    errors = []
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            budget = watch(page, errors, 2)
            page.goto(server + "/")
            page.wait_for_selector("#login:not([hidden])")
            page.fill("#password", "wrong password")
            page.click("#login-form button")
            page.wait_for_selector("#login-error:not(:empty)")
            assert "Wrong password" in page.inner_text("#login-error")
            assert budget[0] == 0 and errors == []                          # the session check and the wrong password: two 401s, on purpose
            page.fill("#password", PASSWORD)
            page.click("#login-form button")
            page.wait_for_selector("#app:not([hidden])")
            page.wait_for_selector("#empty:not([hidden])")
            assert not page.is_visible("#login")

            page.click("#add-drawing")
            page.set_input_files("#upload-file", str(png))
            page.click("#upload-form button[type=submit]")                 # consent not ticked: the browser blocks it
            assert page.locator("#upload-modal").evaluate("d => d.open")
            page.check("#upload-consent")
            page.click("#upload-form button[type=submit]")
            page.wait_for_selector("#rows tr")

            # v1, with an edited prompt
            make_clip(page, "Make clip", prompt="A ghost waves from the end of the hall.")
            page.wait_for_selector('tr[data-status="ready"]', timeout=30000)
            row = page.locator("#rows tr").first
            assert row.locator("td[data-label=Characters] img").count() == 2
            assert row.locator(".clip video").count() == 1
            assert row.locator("td[data-label=Characters] .vlabel").inner_text() == "v1"
            assert row.locator("td[data-label=Clips] .vlabel").inner_text() == "v1"

            public = row.locator(".clip").get_by_role("button", name="Public")
            assert public.is_disabled()                                     # needs the OK online box
            row.get_by_role("button", name="?").first.click()
            assert "OK online" in row.locator(".tip").first.inner_text()    # touch-friendly reason
            row.get_by_label("OK online").check()
            page.wait_for_selector('.clip button:text("Public"):not([disabled])', timeout=10000)
            page.locator('.clip button:text("Public")').click()
            page.wait_for_selector('tr[data-status="public"]')
            assert "Spent $0.000" in page.inner_text("#spend")              # the fake provider spends nothing

            # v2 appears next to v1; publishing v2 replaces v1; pressing the lit button withdraws
            make_clip(page, "Make another clip")
            page.wait_for_selector(".clip >> nth=1", timeout=30000)
            labels = row.locator("td[data-label=Clips] .vlabel").all_inner_texts()
            assert labels == ["v1", "v2"], labels
            assert row.locator("td[data-label=Characters] .vlabel").all_inner_texts() == ["v1", "v2"]
            v1, v2 = row.locator(".clip").nth(0), row.locator(".clip").nth(1)
            assert "active" in (v1.get_by_role("button", name="Public").get_attribute("class") or "")
            v2.get_by_role("button", name="Public").click()
            v2.locator("button.active").wait_for()                          # v2 is now the lit one
            assert "active" not in (v1.get_by_role("button", name="Public").get_attribute("class") or "")
            page.screenshot(path=str(shots / "console-1280.png"), full_page=True)
            v2.get_by_role("button", name="Public").click()
            page.wait_for_selector('tr[data-status="ready"]')

            page.select_option("#filter", "preview")
            assert page.locator("#rows tr").count() == 0
            page.select_option("#filter", "ready")
            assert page.locator("#rows tr").count() == 1
            page.select_option("#filter", "all")
            page.locator("#rows tr .x").click()
            page.get_by_role("button", name="Delete for good").click()
            page.wait_for_selector("#empty:not([hidden])")
        finally:
            browser.close()
    assert errors == [], errors


def test_phone_390_can_do_everything(server, tmp_path):
    """At 390 px every control is on screen and the whole job can be done: no sideways scrolling."""
    shots = Path(os.environ.get("HH_SCREENSHOT_DIR", tmp_path))
    shots.mkdir(parents=True, exist_ok=True)
    png = tmp_path / "drawing.png"
    png.write_bytes(drawing_png())
    errors = []
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 800}, has_touch=True)
            sign_in(page, server, errors)
            add_drawing(page, png)
            make_clip(page, "Make clip")
            page.wait_for_selector('tr[data-status="ready"]', timeout=30000)
            row = page.locator("#rows tr").first
            make_clip(page, "Make another clip")
            page.wait_for_selector(".clip >> nth=1", timeout=30000)

            assert page.evaluate(GEOMETRY) == []
            assert page.evaluate(TOUCH) == [], page.evaluate(TOUCH)         # every target is at least 44 x 44 px
            row.get_by_role("button", name="?").first.tap()                 # the "why is this off" button is a target too
            assert page.evaluate(TOUCH) == [], page.evaluate(TOUCH)
            page.get_by_role("button", name="Make another clip").first.tap()
            page.wait_for_selector("#modal[open] #prompt-box")
            assert page.evaluate(TOUCH) == [], page.evaluate(TOUCH)         # the modal as well
            page.get_by_role("button", name="Cancel").tap()

            row.get_by_label("OK online").tap()
            page.wait_for_selector('.clip button:text("Public"):not([disabled])')
            row.locator(".clip").nth(1).get_by_role("button", name="Public").tap()
            page.wait_for_selector('tr[data-status="public"]')
            assert page.evaluate(GEOMETRY) == []
            page.screenshot(path=str(shots / "console-390.png"), full_page=True)
        finally:
            browser.close()
    assert errors == [], errors
    req = urllib.request.Request(server + "/loop/manifest.json", headers={"Authorization": f"Bearer {LOOP}"})
    assert len(json.load(urllib.request.urlopen(req))["clips"]) == 1        # published from the phone


PAYLOADS = [
    "<img src=x onerror=window.__pwned=1>",
    "</p><script>window.__pwned=2</script>",
    '"><svg onload=window.__pwned=3>',
]


def test_a_hostile_prompt_is_shown_as_text_and_never_runs(server, tmp_path):
    """The prompt is displayed under each clip. Whatever it holds, it must stay text."""
    png = tmp_path / "drawing.png"
    png.write_bytes(drawing_png())
    errors = []
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            sign_in(page, server, errors)
            add_drawing(page, png)
            payload = PAYLOADS[0]
            make_clip(page, "Make clip", prompt=payload)
            page.wait_for_selector('tr[data-status="ready"]', timeout=30000)
            shown = page.locator(".clip .prompt")
            assert shown.inner_text() == payload                           # visible as plain text
            assert shown.get_attribute("title") == payload
            assert page.locator('img[src="x"]').count() == 0              # no element was created from it
            for i, other in enumerate(PAYLOADS[1:], start=1):
                make_clip(page, "Make another clip", prompt=other)
                page.wait_for_selector(f".clip >> nth={i}", timeout=30000)
            assert page.locator(".clip .prompt").all_inner_texts() == PAYLOADS
            assert page.locator("#rows script, #rows svg, #rows img[src='x']").count() == 0
            assert page.evaluate("typeof window.__pwned") == "undefined"
            # the textarea of the next modal shows the default prompt, also as plain text
            page.get_by_role("button", name="Make another clip").first.click()
            page.wait_for_selector("#modal[open] #prompt-box")
            assert "hallway" in page.input_value("#prompt-box")
            assert page.locator("#modal-body script, #modal-body img").count() == 0
        finally:
            browser.close()
    assert errors == [], errors


def test_reload_and_a_second_context_keep_the_session(server, tmp_path):
    png = tmp_path / "drawing.png"
    png.write_bytes(drawing_png())
    errors = []
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            c1 = browser.new_context()
            p1 = c1.new_page()
            sign_in(p1, server, errors)
            add_drawing(p1, png)

            p1.reload()                                                     # same browser, cookie kept
            p1.wait_for_selector("#rows tr")
            assert p1.is_visible("#app") and not p1.is_visible("#login")
            p1.get_by_label("OK online").check()                            # a POST works: the token came from /api/session
            for _ in range(50):
                if p1.evaluate("fetch('/api/drawings').then(r => r.json()).then(j => j.drawings[0].ok_online)"):
                    break
                p1.wait_for_timeout(100)
            assert p1.evaluate("fetch('/api/drawings').then(r => r.json()).then(j => j.drawings[0].ok_online)") is True

            c2 = browser.new_context(storage_state=c1.storage_state())      # another context holding the same cookie
            p2 = c2.new_page()
            watch(p2, errors, 0)
            p2.goto(server + "/")
            p2.wait_for_selector("#rows tr")
            assert p2.is_visible("#app") and not p2.is_visible("#login")

            c3 = browser.new_context()                                      # no cookie at all
            p3 = c3.new_page()
            watch(p3, errors, 3)                                            # page load + the two fetches below
            p3.goto(server + "/")
            p3.wait_for_selector("#login:not([hidden])")
            assert p3.is_hidden("#app")
            assert p3.evaluate("fetch('/api/session').then(r => r.status)") == 401
            assert p3.evaluate("fetch('/api/drawings').then(r => r.status)") == 401
        finally:
            browser.close()
    assert errors == [], errors
