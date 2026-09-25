"""End-to-end browser tests (Playwright) against an isolated test server.

    .venv/bin/python -m pytest -m e2e tests/e2e -v

A separate web server and worker run on port 8101 with their own database
(findmyphotos_e2e) and a temporary data folder. The camera is a fake webcam fed with a
permitted NASA portrait (tests/e2e: generated .y4m video), so no real camera is used.
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psycopg
import pytest
from PIL import Image

from conftest import ADMIN_PASSWORD, NASA, ROOT, SEARCH, SUPER_URL, TEST_URL, _user

pw = pytest.importorskip("playwright.sync_api")
expect = pw.expect

pytestmark = pytest.mark.e2e
PORT = 8101
BASE = f"http://127.0.0.1:{PORT}"
E2E_DB = "findmyphotos_e2e"
KOCH_PHOTOS = ["photo_30.jpg", "photo_16.jpg", "photo_40.jpg"]
OTHERS = ["photo_01.jpg", "photo_12.jpg"]
SELFIE = SEARCH / "search2_koch_2018_portrait.jpg"


def make_fake_camera_video(image: Path, out: Path, width=640, height=480, frames=30) -> Path:
    """A .y4m video (what Chromium's fake webcam plays) showing one still image."""
    canvas = Image.new("RGB", (width, height), (40, 40, 40))
    photo = Image.open(image).convert("RGB")
    photo = photo.crop((0, 0, photo.width, int(photo.height * 0.6)))  # head and shoulders, like a selfie
    photo.thumbnail((width, height))
    canvas.paste(photo, ((width - photo.width) // 2, (height - photo.height) // 2))
    y, cb, cr = canvas.convert("YCbCr").split()
    half = (width // 2, height // 2)
    frame = y.tobytes() + cb.resize(half).tobytes() + cr.resize(half).tobytes()
    with open(out, "wb") as f:
        f.write(f"YUV4MPEG2 W{width} H{height} F30:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(frames):
            f.write(b"FRAME\n" + frame)
    return out


@pytest.fixture(scope="module")
def server():
    with psycopg.connect(SUPER_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {E2E_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {E2E_DB} OWNER {_user}")
    url = TEST_URL.rsplit("/", 1)[0] + "/" + E2E_DB
    env = dict(os.environ, DATABASE_URL=url, DATA_DIR=tempfile.mkdtemp(prefix="fmp-e2e-"), LOG_LEVEL="WARNING",
               SEARCHES_PER_10_MIN="1000", WORKER_THREADS="2")
    subprocess.run([sys.executable, "-m", "app.migrate"], cwd=ROOT, env=env, check=True, capture_output=True)
    web = subprocess.Popen([str(ROOT / ".venv/bin/uvicorn"), "app.main:app", "--port", str(PORT), "--no-access-log"],
                           cwd=ROOT, env=env)
    work = subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=env)
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    yield BASE
    for proc in (web, work):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
    with psycopg.connect(SUPER_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {E2E_DB} WITH (FORCE)")


@pytest.fixture(scope="module")
def fake_video(tmp_path_factory):
    return make_fake_camera_video(SELFIE, tmp_path_factory.mktemp("cam") / "koch.y4m")


@pytest.fixture(scope="module")
def playwright():
    with pw.sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def event_link(server, playwright):
    """Organiser flow in the browser: log in, create an event, upload photos, wait."""
    browser = playwright.chromium.launch()
    page = browser.new_page(bypass_csp=True)
    page.goto(f"{server}/admin/login")
    page.fill("#password", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.fill("input[name=name]", "E2E event")
    page.press("input[name=name]", "Enter")
    page.wait_for_url(re.compile(r"/admin/events/\d+"))
    page.set_input_files("#photo-input", [str(NASA / n) for n in KOCH_PHOTOS + OTHERS])
    page.wait_for_function("document.getElementById('process-text').textContent.startsWith('Обработано 5 из 5')",
                           timeout=60_000)
    assert page.locator("#t-upload").inner_text().endswith("на 5 фотографий")
    assert re.match(r"\d+,\d\d с", page.locator("#t-wall").inner_text())
    assert "на фото" in page.locator("#t-process").inner_text()
    link = page.input_value("#visitor-link")
    browser.close()
    return link


def track_searches(page) -> list:
    calls = []
    page.on("request", lambda r: calls.append(r.url) if r.url.endswith("/search") else None)
    return calls


def finish_search(page, expected_min: int):
    page.check("#consent")
    page.click("#search-btn")
    expect(page.locator("#timer-label")).to_have_text("Время поиска", timeout=30_000)  # works under strict CSP
    assert re.match(r"\d+,\d\d с$", page.locator("#timer-clock").inner_text())
    assert "поиск лица" in page.locator("#timer-detail").inner_text()
    title = page.locator("#results-title").inner_text()
    assert "возможн" in title and int(title.split()[0]) >= expected_min, title
    assert page.locator("#result-grid .badge-warn").first.text_content().strip() == "Возможное совпадение"  # shown in capitals by the design
    assert "удалено со страницы" in page.locator("#message").inner_text()


def choose_camera(page):
    """Desktop: floating button -> "Take photo" (starts the camera). Phones: the tab, then "Turn on camera"."""
    if page.is_visible("#photo-fab"):
        page.click("#photo-fab")
        page.click("#menu-camera")
    else:
        page.click("#tab-camera")
        page.click("#camera-start")


def choose_upload_with_fab(page, photo):
    with page.expect_file_chooser() as chooser:
        page.click("#photo-fab")
        page.click("#menu-upload")
    chooser.value.set_files(str(photo))


def camera_flow(page, link):
    searches = track_searches(page)
    page.goto(link)
    choose_camera(page)
    page.wait_for_function("document.getElementById('camera-video').videoWidth > 0", timeout=15_000)
    page.click("#camera-snap")
    page.wait_for_selector("#camera-shot:not([hidden])")
    assert page.is_visible("#camera-retake")
    if page.is_visible("#photo-fab"):                      # desktop: the panel stays clear of the header
        header = page.locator(".topbar-inner").bounding_box()
        assert page.locator("#search-card").bounding_box()["y"] >= header["y"] + header["height"]
    assert page.evaluate("document.getElementById('camera-video').srcObject") is None  # camera turned off
    assert searches == []                                  # taking the photo does not search
    page.click("#camera-retake")                           # retake works
    page.wait_for_function("document.getElementById('camera-video').videoWidth > 0", timeout=15_000)
    page.click("#camera-snap")
    page.wait_for_selector("#camera-shot:not([hidden])")
    assert searches == []
    finish_search(page, expected_min=2)
    assert len(searches) == 1


def upload_flow(page, link):
    page.goto(link)
    assert page.get_attribute("#tab-upload", "aria-selected") == "true"
    page.set_input_files("#selfie-input", str(SELFIE))
    expect(page.locator("#selfie-preview")).to_be_visible()   # waits until the preview has loaded
    finish_search(page, expected_min=2)
    expect(page.locator("#selfie-preview")).to_be_hidden()    # the photo was removed from the page


def camera_browser(playwright, fake_video):
    return playwright.chromium.launch(args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-video-capture={fake_video}"])


def test_desktop_upload_flow_and_clean_download(playwright, event_link, tmp_path):
    browser = playwright.chromium.launch()
    page = browser.new_page(accept_downloads=True, bypass_csp=True)
    upload_flow(page, event_link)
    with page.expect_download() as dl:
        page.locator("#result-grid a.btn").first.click()
    saved = tmp_path / "result.jpg"
    dl.value.save_as(saved)
    with Image.open(saved) as img:
        assert img.format == "JPEG" and not img.getexif()
    browser.close()


def test_desktop_camera_flow(playwright, event_link, fake_video):
    browser = camera_browser(playwright, fake_video)
    context = browser.new_context(permissions=["camera"], bypass_csp=True)
    page = context.new_page()
    camera_flow(page, event_link)
    assert page.is_visible("#photo-fab") and page.is_hidden("#tab-camera")  # went through the floating button
    assert_results_view_and_back(page)
    assert page.evaluate("document.getElementById('camera-video').srcObject") is None  # camera off
    browser.close()


def test_mobile_camera_and_upload_flows(playwright, event_link, fake_video):
    browser = camera_browser(playwright, fake_video)
    context = browser.new_context(**playwright.devices["Pixel 7"], permissions=["camera"], bypass_csp=True)
    page = context.new_page()
    camera_flow(page, event_link)
    upload_flow(page, event_link)
    assert page.is_hidden("#photo-fab") and page.is_visible("#tab-camera")  # phones keep the tabs
    assert page.is_hidden("#gallery") and page.locator("#gallery-grid img").count() == 0  # desktop-only for now
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")  # no sideways scrolling
    browser.close()


def assert_results_view_and_back(page):
    """Desktop: matches replace the gallery; a match opens in the large view; a button leads back."""
    expect(page.locator("#results")).to_be_visible()
    expect(page.locator("#gallery")).to_be_hidden()
    expect(page.locator("#search-card")).to_be_hidden()
    assert re.match(r"Время поиска \d+,\d\d с$", page.text_content("#results-time"))
    assert "поиск лица" in page.inner_text("#results-timing")
    page.locator("#result-grid .tile-img").first.click()
    expect(page.locator("#viewer")).to_be_visible()
    assert page.inner_text("#viewer-title") == "Возможное совпадение 1" and page.is_visible("#viewer-badge")
    page.keyboard.press("Escape")
    expect(page.locator("#viewer")).to_be_hidden()
    page.click("#results-back")
    expect(page.locator("#gallery")).to_be_visible()
    expect(page.locator("#results")).to_be_hidden()
    assert page.locator("#gallery-grid img").count() == 5


def test_desktop_gallery_shows_all_event_photos(playwright, event_link):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, bypass_csp=True)
    page.goto(event_link)
    expect(page.locator("#gallery-grid img")).to_have_count(5)
    assert page.text_content("#gallery-count") == "5 фотографий"
    assert page.is_hidden("#gallery-status")               # nothing still being prepared
    page.wait_for_function("[...document.querySelectorAll('#gallery-grid img')].every(i => i.complete && i.naturalWidth > 0)")
    for hidden in ("#selfie-drop", "#tab-camera", "#tab-upload", "#search-btn", "#consent"):  # no form on the main screen
        assert page.is_hidden(hidden), hidden
    assert page.is_visible("#photo-fab")
    sizes = page.evaluate("[...document.querySelectorAll('#gallery-grid button')].slice(0, 2).map(b => [b.offsetWidth, b.offsetHeight])")
    assert all(abs(w / h - 1.5) < 0.02 for w, h in sizes)  # 3:2 frames
    before = sizes[0][0]
    page.click("#zoom-in")                                 # thumbnail size control
    assert page.evaluate("document.querySelector('#gallery-grid button').offsetWidth") > before
    page.locator("#gallery-grid button").nth(2).click()   # opens the larger view
    expect(page.locator("#viewer")).to_be_visible()
    page.wait_for_function("document.getElementById('viewer-img').naturalWidth > 0")
    assert page.inner_text("#viewer-count") == "3 из 5"
    assert page.is_hidden("#viewer-badge")                 # not labelled a "possible match"
    photo = lambda: page.get_attribute("#viewer-img", "src").split("?")[0]  # thumbnail first, then the sharp view
    first = photo()
    assert "/photo/" in first and "/photo/" in page.get_attribute("#viewer-download", "href")
    page.keyboard.press("ArrowRight")                      # step through, like a photo app
    assert page.inner_text("#viewer-count") == "4 из 5" and photo() != first
    page.click("#viewer-prev")
    assert page.inner_text("#viewer-count") == "3 из 5" and photo() == first
    page.click("#viewer-close")
    expect(page.locator("#viewer")).to_be_hidden()
    browser.close()


def test_desktop_floating_button_menu(playwright, event_link):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800}, bypass_csp=True)
    page.goto(event_link)
    assert page.is_hidden("#tab-camera") and page.is_hidden("#photo-menu") and page.is_hidden("#search-card")
    box = page.locator("#photo-fab").bounding_box()
    assert 1280 - (box["x"] + box["width"]) < 40 and 800 - (box["y"] + box["height"]) < 40  # bottom-right corner
    page.click("#photo-fab")
    expect(page.locator("#photo-menu")).to_be_visible()
    assert page.get_attribute("#photo-fab", "aria-expanded") == "true"
    assert page.locator("#photo-menu [role=menuitem]").all_inner_texts() == [
        "Сделать фото\nвеб-камерой", "Загрузить фото\nвыбрать файл изображения"]
    assert page.evaluate("document.activeElement.id") == "menu-camera"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.id") == "menu-upload"
    page.keyboard.press("Escape")                          # keyboard: closes and returns to the button
    expect(page.locator("#photo-menu")).to_be_hidden()
    assert page.evaluate("document.activeElement.id") == "photo-fab"
    page.click("#photo-fab")
    page.click("h1")                                       # a click elsewhere closes it
    expect(page.locator("#photo-menu")).to_be_hidden()
    browser.close()


def test_desktop_upload_with_floating_button_and_replace(playwright, event_link):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800}, bypass_csp=True)
    searches = track_searches(page)
    page.goto(event_link)
    choose_upload_with_fab(page, NASA / OTHERS[0])         # first choice: a different photo
    expect(page.locator("#selfie-preview")).to_be_visible()
    first = page.get_attribute("#selfie-preview", "src")
    with page.expect_file_chooser() as chooser:            # "Replace photo" opens the file chooser again
        page.click("#upload-replace")
    chooser.value.set_files(str(SELFIE))
    expect(page.locator("#selfie-preview")).not_to_have_attribute("src", first)
    assert searches == []                                  # choosing a photo does not search
    expect(page.locator("#search-card")).to_be_visible()   # preview, consent and button in the panel
    finish_search(page, expected_min=2)
    assert len(searches) == 1
    expect(page.locator("#selfie-preview")).to_be_hidden()
    expect(page.locator("#upload-replace")).to_be_hidden()
    assert_results_view_and_back(page)
    browser.close()


def test_desktop_grid_keyboard_sphere_and_menu(playwright, event_link):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, bypass_csp=True)
    page.goto(event_link)
    expect(page.locator("#gallery-grid button")).to_have_count(5)
    page.wait_for_selector("#splash", state="detached")
    assert page.evaluate("document.body.classList.contains('motion') && document.body.classList.contains('revealed')")
    # keyboard: one tab stop into the grid, arrows move, Enter opens, Escape returns focus to the photo
    page.locator("#gallery-grid button").first.focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("[...document.querySelectorAll('#gallery-grid button')].indexOf(document.activeElement)") == 2
    assert page.evaluate("[...document.querySelectorAll('#gallery-grid button')].filter(b => b.tabIndex === 0).length") == 1
    page.keyboard.press("Enter")
    expect(page.locator("#viewer")).to_be_visible()
    assert page.inner_text("#viewer-count") == "3 из 5"
    page.keyboard.press("Escape")
    expect(page.locator("#viewer")).to_be_hidden()
    assert page.evaluate("[...document.querySelectorAll('#gallery-grid button')].indexOf(document.activeElement)") == 2
    # sphere view: drag to turn, Enter opens the photo in front, back to the grid
    page.click("#view-sphere")
    expect(page.locator("#sphere")).to_be_visible()
    expect(page.locator(".card3d")).to_have_count(5)
    before = page.evaluate("document.getElementById('world').style.transform")
    box = page.locator("#stage").bounding_box()
    page.mouse.move(box["x"] + 200, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 420, box["y"] + box["height"] / 2, steps=8)
    page.mouse.up()
    assert page.evaluate("document.getElementById('world').style.transform") != before
    page.focus("#stage")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("Enter")
    expect(page.locator("#viewer")).to_be_visible()
    page.click("#viewer-close")
    expect(page.locator("#viewer")).to_be_hidden()
    # full-screen menu: opens, Escape closes it, "All Photos" returns to the grid
    page.click("#menu-btn")
    assert page.get_attribute("#menu-btn", "aria-expanded") == "true"
    expect(page.locator(".menu-link").first).to_be_focused()
    page.keyboard.press("Escape")
    assert page.get_attribute("#menu-btn", "aria-expanded") == "false"
    page.click("#menu-btn")
    page.locator(".menu-link", has_text="Все фотографии").click()
    expect(page.locator("#gallery-grid")).to_be_visible()
    expect(page.locator("#sphere")).to_be_hidden()
    browser.close()


def test_reduced_motion_shows_everything_at_once(playwright, event_link):
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce", bypass_csp=True)
    page = context.new_page()
    page.goto(event_link)
    assert page.locator("#splash").count() == 0            # no title card
    assert not page.evaluate("document.body.classList.contains('motion')")
    assert page.evaluate("getComputedStyle(document.querySelector('#event-title .word')).opacity") == "1"
    assert page.is_hidden("#dot")
    expect(page.locator("#gallery-grid button")).to_have_count(5)
    page.locator("#gallery-grid button").first.click()     # no fly-in animation, opens at once
    expect(page.locator("#viewer")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#viewer")).to_be_hidden()
    browser.close()


def test_admin_pages_use_the_same_design(playwright, server, event_link):
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", bypass_csp=True)
    page = context.new_page()
    pages = _all_pages(page, server, event_link)
    for name in ("admin list", "event detail"):
        page.goto(pages[name])
        style = page.evaluate("""() => ({ h1: getComputedStyle(document.querySelector('h1')).fontFamily,
            body: getComputedStyle(document.body).fontFamily, bg: getComputedStyle(document.body).backgroundColor,
            fonts: document.fonts.check('16px "Playfair Display"') })""")
        assert "Playfair Display" in style["h1"] and "Inter" in style["body"] and style["bg"] == DARK_BG, (name, style)
    assert page.inner_text("#process-text").startswith("Обработано 5 из 5")   # the admin page still works
    browser.close()


def test_large_uploaded_selfie_is_reduced_before_sending(playwright, event_link, tmp_path):
    """A big photo (like a 24-megapixel camera file) is shrunk in the browser, so the request stays
    far below hosting limits such as Vercel's 4.5 MB, and the search still finds the person."""
    big = tmp_path / "big_selfie.jpg"
    with Image.open(SELFIE) as img:
        img.convert("RGB").resize((6000, int(6000 * img.height / img.width))).save(big, quality=97)
    assert big.stat().st_size > 4.5 * 1024 * 1024
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 900}, bypass_csp=True)
    sizes = []
    page.on("request", lambda r: sizes.append(len(r.post_data_buffer or b"")) if r.url.endswith("/search") else None)
    page.goto(event_link)
    choose_upload_with_fab(page, big)
    expect(page.locator("#selfie-preview")).to_be_visible()
    finish_search(page, expected_min=2)
    assert sizes and max(sizes) < 1.5 * 1024 * 1024, sizes
    browser.close()


@pytest.mark.parametrize("error_name,expected", [
    ("NotAllowedError", "Доступ к камере запрещён"),        # what browsers report when the visitor clicks "Block"
    ("NotFoundError", "не найдена камера"),
    ("NotReadableError", "занята другим приложением"),
])
def test_camera_errors_show_clear_messages(playwright, event_link, error_name, expected):
    """Simulated: headless test browsers cannot show a real permission prompt, so the camera
    request is made to fail with the exact error a real browser gives in each situation."""
    browser = playwright.chromium.launch()
    context = browser.new_context(bypass_csp=True)
    context.add_init_script(f"navigator.mediaDevices.getUserMedia = () => "
                            f"Promise.reject(new DOMException('simulated', '{error_name}'));")
    page = context.new_page()
    page.goto(event_link)
    choose_camera(page)
    page.wait_for_selector("#camera-error:not([hidden])", timeout=10_000)
    assert expected in page.locator("#camera-error").inner_text()
    assert page.is_disabled("#search-btn")
    browser.close()


def test_camera_unsupported_in_this_browser_shows_clear_message(playwright, event_link):
    """Real behaviour of headless Chromium without camera permission (NotSupportedError)."""
    browser = playwright.chromium.launch(args=["--use-fake-device-for-media-stream"])
    page = browser.new_page(bypass_csp=True)
    page.goto(event_link)
    choose_camera(page)
    page.wait_for_selector("#camera-error:not([hidden])", timeout=10_000)
    assert "не может использовать камеру" in page.locator("#camera-error").inner_text()
    browser.close()


def test_camera_is_not_left_on_when_switching_tabs_during_the_permission_prompt(playwright, event_link, fake_video):
    browser = camera_browser(playwright, fake_video)
    context = browser.new_context(permissions=["camera"], bypass_csp=True)
    context.add_init_script("""
        const real = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
        window.__streams = [];
        navigator.mediaDevices.getUserMedia = async (c) => {
          await new Promise(r => setTimeout(r, 800));   // a slow permission prompt
          const s = await real(c); window.__streams.push(s); return s;
        };""")
    page = context.new_page()
    page.goto(event_link)
    choose_camera(page)
    with page.expect_file_chooser():                       # leave before the camera answers
        page.click("#photo-fab")
        page.click("#menu-upload")
    page.wait_for_timeout(1500)
    states = page.evaluate("window.__streams.flatMap(s => s.getTracks().map(t => t.readyState))")
    assert states and all(s == "ended" for s in states)
    browser.close()


DARK_BG, DARK_CARD = "rgb(0, 0, 0)", "rgb(11, 11, 11)"  # the black archive theme


def _page_colours(page):
    return page.evaluate("""() => ({
        html: getComputedStyle(document.documentElement).backgroundColor,
        body: getComputedStyle(document.body).backgroundColor,
        card: document.querySelector('.card') ? getComputedStyle(document.querySelector('.card')).backgroundColor : null,
        scheme: getComputedStyle(document.documentElement).colorScheme })""")


def _all_pages(page, server, event_link):
    """Admin list, event detail, visitor page and login page, with an admin session."""
    page.goto(f"{server}/admin/login")
    page.fill("#password", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{server}/admin")
    detail = server + page.locator("a.event-row").first.get_attribute("href")
    return {"admin list": f"{server}/admin", "event detail": detail, "visitor": event_link,
            "home": f"{server}/"}


@pytest.mark.parametrize("how", ["device in dark mode", "Dark chosen in the Theme switch"])
def test_dark_theme_is_consistent_on_every_page(playwright, server, event_link, how):
    browser = playwright.chromium.launch()
    if how == "device in dark mode":
        context = browser.new_context(color_scheme="dark", bypass_csp=True)
    else:
        context = browser.new_context(color_scheme="light", bypass_csp=True)
        context.add_init_script("localStorage.setItem('fmp-theme', 'dark')")
    page = context.new_page()
    for name, url in _all_pages(page, server, event_link).items():
        page.goto(url)
        colours = _page_colours(page)
        assert colours["html"] == DARK_BG and colours["body"] == DARK_BG, (name, colours)
        assert colours["card"] in (None, DARK_CARD), (name, colours)
        assert colours["scheme"] == "dark", (name, colours)
    browser.close()


def test_theme_switch_cycles_and_is_remembered(playwright, server, event_link):
    browser = playwright.chromium.launch()
    context = browser.new_context(color_scheme="light", bypass_csp=True)
    page = context.new_page()
    page.goto(event_link)
    assert page.inner_text("#theme-toggle") == "Тема: авто"
    page.click("#theme-toggle")
    assert page.inner_text("#theme-toggle") == "Тема: тёмная" and _page_colours(page)["body"] == DARK_BG
    page.goto(f"{server}/admin/login")                     # another page keeps the choice
    assert _page_colours(page)["body"] == DARK_BG
    page.click("#theme-toggle")
    assert page.inner_text("#theme-toggle") == "Тема: светлая" and _page_colours(page)["body"] != DARK_BG
    browser.close()


def test_iphone_safari_upload_flow(playwright, event_link):
    try:
        browser = playwright.webkit.launch()
    except Exception as exc:  # WebKit build not installed
        pytest.skip(f"WebKit not available: {exc}")
    context = browser.new_context(**playwright.devices["iPhone 13"], bypass_csp=True)
    upload_flow(context.new_page(), event_link)
    browser.close()
