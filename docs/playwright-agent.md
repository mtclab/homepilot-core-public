# Playwright Agent Guide — HomePilot v2

Interactive web UI testing using the project venv. Reference for AI agents writing or running Playwright scripts.

---

## Setup

All Playwright tooling lives in the project venv. Never use a global install.

```bash
cd $HOME/repot/homepilot-v2

# Run a script
.venv/bin/python my_script.py

# Run e2e tests
.venv/bin/pytest tests/test_e2e.py -v

# Playwright CLI (e.g. codegen, screenshot)
.venv/bin/playwright <command>
```

Browser: Chromium. Already installed. No `playwright install` needed.

Display for headed (visible) mode:

```python
import os

os.environ["DISPLAY"] = ":1.0"
```

---

## Boilerplate

```python
import os
from playwright.sync_api import sync_playwright

os.environ["DISPLAY"] = ":1.0"  # omit for headless

TOKEN = "hp_REDACTED_TEST_TOKEN"
BASE_URL = "http://homepilot:8000"
UI = BASE_URL + "/ui"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = browser.new_context(ignore_https_errors=True)

    # Inject auth cookie so every page load is authenticated
    ctx.add_cookies([{"name": "hp_token", "value": TOKEN, "domain": "homepilot", "path": "/"}])

    page = ctx.new_page()
    # ... do work ...
    browser.close()
```

---

## Navigation

```python
page.goto(UI + "/artifacts")  # navigate, wait for load event
page.goto(UI + "/inventory", wait_until="networkidle")  # wait until no network activity
page.go_back()
page.go_forward()
page.reload()
print(page.url)  # current URL
print(page.title())  # page <title>
```

`wait_until` options: `"load"` (default), `"domcontentloaded"`, `"networkidle"`, `"commit"`.

---

## Locators

Prefer role > text > CSS. Avoid XPath.

```python
# By ARIA role + accessible name
page.get_by_role("button", name="Save")
page.get_by_role("link", name="Artifacts")
page.get_by_role("heading", name="Settings")
page.get_by_role("textbox", name="API Token")
page.get_by_role("checkbox", name="Enable drift")

# By label text
page.get_by_label("API Token")

# By visible text
page.get_by_text("Session active")
page.get_by_text("No artifacts found", exact=True)

# By placeholder
page.get_by_placeholder("Search...")

# By CSS selector
page.locator("input#token")
page.locator("nav a.active")
page.locator(".artifact-card:first-child")
page.locator("table tbody tr")  # all rows

# Chain: scope child within parent
page.locator(".settings-panel").get_by_role("button", name="Save")

# nth match (0-indexed)
page.locator("tr").nth(2)

# filter by text
page.locator("tr").filter(has_text="proxmox-01")
```

---

## Clicks

```python
page.get_by_role("button", name="Save").click()
page.get_by_role("link", name="Inventory").click()

# Click coordinates relative to element
page.locator(".map").click(position={"x": 100, "y": 50})

# Right-click
page.locator(".item").click(button="right")

# Double-click
page.locator(".editable").dbl_click()

# Hover (triggers CSS hover states, tooltips)
page.locator(".tooltip-trigger").hover()

# Force click (bypasses actionability checks — use sparingly)
page.locator("button").click(force=True)
```

---

## Filling Forms

```python
# Clear + type into input/textarea
page.get_by_label("API Token").fill("hp_abc123")
page.locator("input#token").fill("hp_abc123")

# Type character by character (triggers keydown/keypress/keyup events)
page.locator("input#search").type("proxmox")

# Clear a field
page.locator("input#token").clear()

# Select from <select> dropdown
page.locator("select#scope").select_option("read")
page.locator("select#scope").select_option(label="Read only")
page.locator("select#scope").select_option(index=1)

# Check / uncheck checkbox
page.get_by_role("checkbox", name="Notify on drift").check()
page.get_by_role("checkbox", name="Notify on drift").uncheck()
page.get_by_role("checkbox").is_checked()  # → bool

# Upload file
page.locator("input[type=file]").set_input_files("/path/to/file.yaml")
```

---

## Keyboard

```python
# Press a key on the focused element
page.keyboard.press("Enter")
page.keyboard.press("Tab")
page.keyboard.press("Escape")
page.keyboard.press("Control+a")  # select all
page.keyboard.press("Control+c")  # copy

# Type text at current focus (no element needed)
page.keyboard.type("some text")

# Focus an element, then type
page.locator("input#search").focus()
page.keyboard.type("proxmox")
page.keyboard.press("Enter")
```

---

## Waiting

Playwright auto-waits for elements to be actionable before clicks/fills. For explicit waits:

```python
# Wait for element to appear and be visible
page.wait_for_selector("text=Session active")
page.locator(".toast").wait_for(state="visible")

# Wait for element to disappear
page.locator(".spinner").wait_for(state="hidden")
page.locator(".modal").wait_for(state="detached")

# Wait for URL change
page.wait_for_url("**/ui/artifacts**")
page.wait_for_url(lambda url: "/artifacts" in url)

# Wait for network idle (all requests done)
page.wait_for_load_state("networkidle")

# Wait for a specific response
with page.expect_response("**/api/inventory") as resp:
    page.get_by_role("button", name="Refresh").click()
resp.value.json()  # response body

# Wait for navigation triggered by a click
with page.expect_navigation():
    page.get_by_role("link", name="Artifacts").click()
```

---

## Reading Page Content

```python
# Full visible text of an element
text = page.locator("body").inner_text()
text = page.locator(".artifact-title").inner_text()

# HTML content
html = page.locator(".card").inner_html()

# Attribute value
href = page.locator("a.logo").get_attribute("href")
cls = page.locator("button").get_attribute("class")

# Input value
val = page.locator("input#token").input_value()

# Count matching elements
n = page.locator("tr").count()

# Check visibility / existence
page.locator(".error").is_visible()  # → bool
page.locator(".error").is_hidden()  # → bool
page.locator(".error").count() > 0  # element exists in DOM
```

---

## Assertions

Use `expect()` — these retry automatically until timeout.

```python
from playwright.sync_api import expect

expect(page).to_have_url("http://homepilot:8000/ui/artifacts")
expect(page).to_have_title("HomePilot")

expect(page.locator("h1")).to_have_text("Artifacts")
expect(page.locator(".status")).to_contain_text("Session active")
expect(page.locator("input#token")).to_have_value("hp_abc")

expect(page.locator(".error")).to_be_hidden()
expect(page.locator("button[type=submit]")).to_be_enabled()
expect(page.locator("button[type=submit]")).to_be_disabled()
expect(page.locator(".spinner")).to_be_visible()

# Count
expect(page.locator("tr")).to_have_count(5)

# Custom timeout
expect(page.locator(".result")).to_be_visible(timeout=10_000)
```

---

## Screenshots & Debugging

```python
# Screenshot of full page
page.screenshot(path="debug.png", full_page=True)

# Screenshot of element only
page.locator(".card").screenshot(path="card.png")

# Dump page content for inspection
print(page.content())  # full HTML
print(page.locator("nav").inner_html())

# Pause execution (opens interactive inspector — headed mode only)
page.pause()

# Console messages
page.on("console", lambda msg: print(f"[{msg.type}] {msg.text}"))

# Intercept requests
page.on("request", lambda req: print(">>", req.method, req.url))
page.on("response", lambda resp: print("<<", resp.status, resp.url))
```

---

## API Requests (same session)

Make HTTP requests from within the browser context — shares cookies.

```python
# GET
resp = page.request.get(f"{BASE_URL}/health")
assert resp.ok
body = resp.json()

# GET with auth header
resp = page.request.get(f"{BASE_URL}/artifacts", headers={"Authorization": f"Bearer {TOKEN}"})

# POST JSON
resp = page.request.post(
    f"{BASE_URL}/auth/login",
    data='{"token": "hp_..."}',
    headers={"Content-Type": "application/json"},
)
```

---

## Running Tests

```bash
cd $HOME/repot/homepilot-v2

# All e2e tests
HP_TEST_TOKEN=hp_REDACTED_TEST_TOKEN \
  .venv/bin/pytest tests/test_e2e.py -v

# Single test class
HP_TEST_TOKEN=hp_... .venv/bin/pytest tests/test_e2e.py::TestUIPages -v

# Single test
HP_TEST_TOKEN=hp_... .venv/bin/pytest tests/test_e2e.py::TestUIPages::test_artifacts_page_loads -v

# Keep browser open on failure (headed)
DISPLAY=:1.0 HP_TEST_TOKEN=hp_... .venv/bin/pytest tests/test_e2e.py -v --headed

# Save trace for debugging
HP_TEST_TOKEN=hp_... .venv/bin/pytest tests/test_e2e.py -v \
  --tracing=on --output=test-results/
```

---

## Common Patterns

### Login via UI

```python
page.goto(UI + "/settings")
page.get_by_label("API Token").fill(TOKEN)  # or: page.locator("input#token").fill(TOKEN)
page.get_by_role("button", name="Save").click()
page.wait_for_url("**/artifacts**")
```

### Login via cookie (faster — skip UI)

```python
ctx.add_cookies([{"name": "hp_token", "value": TOKEN, "domain": "homepilot", "path": "/"}])
```

### Check all nav routes for errors

```python
routes = ["artifacts", "inventory", "kb", "drift", "review", "journal", "settings"]
for route in routes:
    page.goto(f"{UI}/{route}", wait_until="networkidle")
    body = page.locator("body").inner_text()
    assert "401" not in body, f"{route}: got 401"
    assert "Missing credentials" not in body, f"{route}: missing credentials"
```

### Wait for toast / flash message

```python
page.get_by_role("button", name="Save").click()
expect(page.locator(".toast, [role='alert']")).to_be_visible(timeout=5_000)
msg = page.locator(".toast").inner_text()
```
