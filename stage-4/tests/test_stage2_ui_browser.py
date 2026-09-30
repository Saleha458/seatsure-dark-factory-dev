"""Edge integration coverage for Stage 2 auth, search and responsive navigation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request

from test_stage2 import STAGE2, combined_fixture, free_port, http_call, start_service
from test_stage2_browser import DevToolsPage, EDGE, NODE


@unittest.skipUnless(EDGE and NODE, "Microsoft Edge and Node.js are required for browser tests")
class Stage2SearchBrowser(unittest.TestCase):
    def test_full_browser_user_flow(self):
        port = free_port()
        service = start_service(STAGE2, port)
        browser = None
        profile = tempfile.TemporaryDirectory(prefix="tablekeeper-ui-edge-")
        edge_process = None
        try:
            base = f"http://127.0.0.1:{port}"
            fixture = combined_fixture()
            closed = dict(fixture["restaurants"][0])
            closed.update(id="r_closed", name="The Quiet Room", opening_hours=[])
            fixture["restaurants"].append(closed)
            http_call(base, "POST", "/_test/reset", fixture)

            debug_port = free_port()
            edge_process = subprocess.Popen([
                EDGE, "--headless=new", "--disable-gpu", "--disable-background-networking",
                "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile.name}",
                "about:blank",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            target = None
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{debug_port}/json/list", timeout=1
                    ) as response:
                        pages = json.loads(response.read())
                    target = next(page for page in pages if page.get("type") == "page")
                    break
                except (OSError, StopIteration):
                    if edge_process.poll() is not None:
                        self.fail(f"Edge exited with code {edge_process.returncode}")
                    time.sleep(0.1)
            self.assertIsNotNone(target, "Edge DevTools did not start")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{debug_port}/json/version", timeout=2
            ) as response:
                browser_info = json.loads(response.read())
            browser = DevToolsPage(browser_info["webSocketDebuggerUrl"], target["id"])
            browser.call("Runtime.enable")
            browser.call("Emulation.setDeviceMetricsOverride", {
                "width": 375, "height": 812, "deviceScaleFactor": 1, "mobile": False,
            })
            browser.call("Page.navigate", {"url": base + "/"})
            browser.wait("location.pathname === '/' && !!document.querySelector('[data-testid=restaurant-select]')")
            browser.wait("document.querySelector('[data-testid=restaurant-select] option[value=r1]') !== null")
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            browser.evaluate("""
              document.querySelector('[data-testid=restaurant-select]').value='r1';
              document.querySelector('[data-testid=date-input]').value='2035-09-24';
              document.querySelector('[data-testid=party-size-input]').value='6';
              document.querySelector('[data-testid=search-button]').click();
            """)
            browser.wait("document.querySelector('[data-testid=\"slot-t2-18:00\"]') !== null")
            browser.evaluate("document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]').click()")
            browser.wait("!!document.querySelector('[data-testid=auth-error]')")
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-form]')"))
            browser.call("Page.navigate", {"url": base + "/signup"})
            browser.wait("location.pathname === '/signup' && !!document.querySelector('[data-testid=signup-submit]')")
            browser.evaluate("""
              (() => {
                const nativeFetch = window.fetch.bind(window);
                window.__availabilityStarted = [];
                window.__slowDate = "";
                window.__slowFinished = false;
                window.__failAvailability = false;
                window.fetch = async (input, init = {}) => {
                  const url = typeof input === "string" ? input : input.url;
                  if (url.startsWith("/availability")) {
                    const date = new URL(url, location.href).searchParams.get("date");
                    window.__availabilityStarted.push(date);
                    if (window.__failAvailability) {
                      window.__failAvailability = false;
                      throw new TypeError("simulated availability network failure");
                    }
                    if (date === window.__slowDate) {
                      await new Promise(resolve => setTimeout(resolve, 700));
                      window.__slowFinished = true;
                    }
                  }
                  return nativeFetch(input, init);
                };
              })();
            """)
            self.assertTrue(browser.evaluate(
                "!!document.querySelector('[data-testid=signup-email]') && "
                "!!document.querySelector('label[for=signup-email]')"))
            self.assertTrue(browser.evaluate(
                "getComputedStyle(document.querySelector('[data-testid=signup-email]')).outlineStyle === 'none'"))
            browser.evaluate("document.querySelector('[data-testid=signup-email]').focus()")
            self.assertEqual(browser.evaluate(
                "getComputedStyle(document.querySelector('[data-testid=signup-email]')).outlineWidth"), "3px")
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            browser.evaluate("""
              document.querySelector('[data-testid=signup-display-name]').value='Taylor';
              document.querySelector('[data-testid=signup-email]').value='taylor@example.test';
              document.querySelector('[data-testid=signup-password]').value='correct horse';
              document.querySelector('#auth-form').requestSubmit();
            """)
            browser.wait("location.pathname === '/' && !!document.querySelector('[data-testid=current-user]')")
            browser.wait("document.querySelector('[data-testid=restaurant-select] option[value=r1]') !== null")
            self.assertIn("Taylor", browser.evaluate(
                "document.querySelector('[data-testid=current-user]').textContent"))
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=logout-button]')"))

            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=restaurant-select]')"))
            self.assertEqual(browser.evaluate("""
              Array.from(document.querySelectorAll('[data-testid=restaurant-select] option')).map(o=>o.value)
            """), ["", "r1", "r_closed"])
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=date-input]').type"), "date")
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=party-size-input]').type"), "number")
            self.assertTrue(browser.evaluate(
                "!!document.querySelector('label[for=restaurant-select]') && "
                "!!document.querySelector('label[for=date-input]') && "
                "!!document.querySelector('label[for=party-size-input]')"))

            browser.evaluate("""
              window.__slowDate='2035-09-24';
              document.querySelector('[data-testid=restaurant-select]').value='r1';
              document.querySelector('[data-testid=date-input]').value='2035-09-24';
              document.querySelector('[data-testid=party-size-input]').value='6';
              document.querySelector('[data-testid=search-button]').click();
            """)
            browser.wait("window.__availabilityStarted.includes('2035-09-24')")
            self.assertTrue(browser.evaluate(
                "document.querySelector('#search-results').getAttribute('aria-busy') === 'true' && "
                "!!document.querySelector('[data-testid=search-loading][role=status]')"))
            browser.evaluate("""
              document.querySelector('[data-testid=date-input]').value='2035-09-25';
              document.querySelector('[data-testid=party-size-input]').value='2';
              document.querySelector('[data-testid=search-button]').click();
            """)
            browser.wait("document.querySelector('[data-testid=\"slot-t1-18:00\"]') !== null")
            browser.wait("window.__slowFinished")
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=date-input]').value"), "2035-09-25")
            self.assertEqual(browser.evaluate(
                "document.querySelector('[data-testid=\"slot-t1-18:00\"]').dataset.available"), "true")
            self.assertEqual(browser.evaluate(
                "document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]').dataset.available"), "true")
            availability_matches = browser.evaluate("""(async () => {
              const query = new URLSearchParams({
                restaurant_id:'r1', date:'2035-09-25', party_size:'2'
              });
              const response = await fetch(`/availability?${query}`);
              const data = await response.json();
              const slot = data.slots.find(item => item.starts_at_local.endsWith('T18:00'));
              const singles = Array.from(document.querySelectorAll(
                '[data-testid^="slot-"][data-testid$="-18:00"]:not([data-testid*="+"])'
              )).map(cell => {
                const id = cell.dataset.option;
                return cell.dataset.available === String(slot.available_table_ids.includes(id));
              });
              const pairOptions = slot.available_options.filter(option => option.table_ids.length === 2);
              const pairCells = Array.from(document.querySelectorAll(
                '[data-testid^="slot-"][data-testid*="+"][data-testid$="-18:00"]'
              ));
              return singles.every(Boolean) && pairCells.length === pairOptions.length &&
                pairCells.every(cell => pairOptions.some(option =>
                  option.table_ids.join('+') === cell.dataset.option && cell.dataset.available === 'true'));
            })()""")
            self.assertTrue(availability_matches, "Grid cells must match server-authoritative availability")
            self.assertIn("Table 1 & Table 2", browser.evaluate(
                "document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]').textContent"))
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            browser.evaluate("document.querySelector('[data-testid=\"slot-t1-18:00\"]').click()")
            browser.wait("!!document.querySelector('[data-testid=booking-form]')")
            self.assertIn("2035-09-25 at 18:00", browser.evaluate(
                "document.querySelector('[data-testid=booking-summary]').textContent"))

            browser.evaluate("""
              window.__failAvailability=true;
              document.querySelector('[data-testid=date-input]').value='2035-09-26';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("!!document.querySelector('[data-testid=search-error]')")
            self.assertFalse(browser.evaluate("document.querySelector('#search-results').getAttribute('aria-busy') === 'true'"))

            browser.evaluate("""
              document.querySelector('[data-testid=restaurant-select]').value='r_closed';
              document.querySelector('[data-testid=date-input]').value='2035-09-25';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("!!document.querySelector('[data-testid=no-slots]')")

            browser.call("Page.navigate", {"url": base + "/login"})
            browser.wait("location.pathname === '/login' && !!document.querySelector('[data-testid=login-submit]')")
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=current-user]')"))
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=logout-button]')"))
            browser.call("Page.navigate", {"url": base + "/lookup"})
            browser.wait("location.pathname === '/lookup' && !!document.querySelector('[data-testid=lookup-submit]')")
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=current-user]')"))
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=logout-button]')"))
            self.assertTrue(browser.evaluate("!!document.querySelector('label[for=lookup-reference-input]')"))
            browser.evaluate("document.querySelector('[data-testid=logout-button]').click()")
            browser.wait("!document.querySelector('[data-testid=current-user]')")
            self.assertTrue(browser.evaluate(
                "!!document.querySelector('.main-nav a[href=\"/login\"]')"))
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))

            browser.call("Page.navigate", {"url": base + "/login"})
            browser.wait("location.pathname === '/login' && !!document.querySelector('[data-testid=login-submit]')")
            self.assertTrue(browser.evaluate("!!document.querySelector('label[for=login-email]')"))
            browser.evaluate("""
              document.querySelector('[data-testid=login-email]').value='missing@example.test';
              document.querySelector('[data-testid=login-password]').value='incorrect';
              document.querySelector('#auth-form').requestSubmit();
            """)
            browser.wait("!!document.querySelector('[data-testid=auth-error]')")
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=auth-error]').getAttribute('role')"), "alert")
        finally:
            if browser is not None:
                browser.close()
            if edge_process is not None and edge_process.poll() is None:
                edge_process.terminate()
                edge_process.wait(timeout=10)
            profile.cleanup()
            if service.poll() is None:
                service.terminate()
                service.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
