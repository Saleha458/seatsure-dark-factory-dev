"""Browser-level booking, recovery, navigation and mobile-layout checks."""
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

NODE = shutil.which("node")

EDGE = next((str(path) for path in (
    Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) /
    "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", "C:\\Program Files")) /
    "Microsoft/Edge/Application/msedge.exe",
) if path.is_file()), shutil.which("msedge"))


class DevToolsPage:
    def __init__(self, websocket_url, target_id):
        client = Path(__file__).with_name("devtools_client.js")
        self.process = subprocess.Popen(
            [NODE, str(client), websocket_url], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        ready = self.process.stdout.readline()
        if ready != '{"ready":true}\n':
            self.process.terminate()
            raise RuntimeError(f"DevTools client failed to connect: {ready}")
        self.command_id = 0
        attached = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.session_id = attached["sessionId"]

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()

    def call(self, method, params=None):
        self.command_id += 1
        command_id = self.command_id
        command = {"id": command_id, "method": method, "params": params or {}}
        if hasattr(self, "session_id"):
            command["sessionId"] = self.session_id
        self.process.stdin.write(json.dumps(command) + "\n")
        self.process.stdin.flush()
        message = json.loads(self.process.stdout.readline())
        if message.get("id") != command_id:
            raise RuntimeError(f"Unexpected DevTools response: {message}")
        if "error" in message:
            raise RuntimeError(message["error"])
        return message.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })["result"]
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("value")

    def wait(self, expression, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression):
                    return
            except (OSError, ConnectionError):
                pass
            time.sleep(0.05)
        raise AssertionError(f"Timed out waiting for browser condition: {expression}")


@unittest.skipUnless(EDGE and NODE, "Microsoft Edge and Node.js are required for browser tests")
class Stage2BrowserFlows(unittest.TestCase):
    def test_search_races_booking_recovery_auth_lookup_and_mobile_layout(self):
        port = free_port()
        service = start_service(STAGE2, port)
        browser = None
        profile = tempfile.TemporaryDirectory(prefix="tablekeeper-edge-")
        edge_process = None
        try:
            base = f"http://127.0.0.1:{port}"
            fixture = combined_fixture()
            fixture["restaurants"][0]["tables"].extend([
                {"id": "opaque|single", "label": "Pipe Single", "capacity": 2},
                {"id": "opaque|left", "label": "Pipe Left", "capacity": 2},
                {"id": "opaque|right", "label": "Pipe Right", "capacity": 2},
            ])
            fixture["restaurants"][0]["combinable"].append(["opaque|left", "opaque|right"])
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
                    with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as response:
                        pages = json.loads(response.read())
                    target = next(page for page in pages if page.get("type") == "page")
                    break
                except (OSError, StopIteration):
                    if edge_process.poll() is not None:
                        self.fail(f"Edge exited with code {edge_process.returncode}")
                    time.sleep(0.1)
            self.assertIsNotNone(target, "Edge DevTools did not start")
            with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=2) as response:
                browser_info = json.loads(response.read())
            browser = DevToolsPage(browser_info["webSocketDebuggerUrl"], target["id"])
            browser.call("Emulation.setDeviceMetricsOverride", {
                "width": 375, "height": 812, "deviceScaleFactor": 1, "mobile": False,
            })
            browser.call("Page.navigate", {"url": base + "/signup"})
            browser.wait("location.pathname === '/signup' && !!document.querySelector('[data-testid=signup-submit]')")
            browser.evaluate("""
              (() => {
                const nativeFetch = window.fetch.bind(window);
                window.__requests = [];
                window.__availabilityStarted = [];
                window.__slowDate = "";
                window.__faultMode = "";
                window.fetch = async (input, init = {}) => {
                  const url = typeof input === "string" ? input : input.url;
                  const method = init.method || "GET";
                  if (url.startsWith("/availability")) {
                    window.__availabilityStarted.push(url);
                    const date = new URL(url, location.href).searchParams.get("date");
                    if (date && date === window.__slowDate) await new Promise(resolve => setTimeout(resolve, 900));
                  }
                  if (url === "/reservations" && method === "POST") {
                    const headers = new Headers(init.headers || {});
                    const item = {key: headers.get("Idempotency-Key"), body: init.body};
                    window.__requests.push(item);
                    if (window.__faultMode === "before") {
                      window.__faultMode = "";
                      throw new TypeError("simulated lost request");
                    }
                    const response = await nativeFetch(input, init);
                    item.status = response.status;
                    if (!response.ok) {
                      const payload = await response.clone().json();
                      item.errorCode = payload?.error?.code;
                    }
                    if (window.__faultMode === "after") {
                      window.__faultMode = "";
                      throw new TypeError("simulated lost response");
                    }
                    return response;
                  }
                  return nativeFetch(input, init);
                };
              })();
            """)
            self.assertTrue(browser.evaluate("!!document.querySelector('label[for=signup-email]')"))
            browser.evaluate("""
              document.querySelector('[data-testid=signup-display-name]').value='Taylor';
              document.querySelector('[data-testid=signup-email]').value='taylor@example.test';
              document.querySelector('[data-testid=signup-password]').value='correct horse';
              document.querySelector('#auth-form').requestSubmit();
            """)
            browser.wait("location.pathname === '/' && !!document.querySelector('[data-testid=current-user]')")
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))

            browser.evaluate("""
              window.__slowDate='2035-09-24';
              document.querySelector('[data-testid=restaurant-select]').value='r1';
              document.querySelector('[data-testid=date-input]').value='2035-09-24';
              document.querySelector('[data-testid=party-size-input]').value='6';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("window.__availabilityStarted.some(url => url.includes('date=2035-09-24'))")
            browser.evaluate("""
              document.querySelector('[data-testid=date-input]').value='2035-09-25';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]') !== null")
            browser.wait("window.__availabilityStarted.some(url => url.includes('date=2035-09-25'))")
            browser.wait("window.__availabilityStarted.length >= 3")
            time.sleep(1.0)
            browser.evaluate("document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]').click()")
            browser.wait("document.querySelector('[data-testid=booking-form]') !== null")
            self.assertIn("2035-09-25", browser.evaluate(
                "document.querySelector('[data-testid=booking-summary]').textContent"))
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))

            token = browser.evaluate("JSON.parse(localStorage.getItem('tablekeeper-session')).token")
            blocker = {"restaurant_id": "r1", "table_ids": ["t1", "t2"],
                       "starts_at_local": "2035-09-25T18:00", "party_size": 6}
            status, _, _, _ = http_call(base, "POST", "/reservations", blocker, {
                "Authorization": "Bearer " + token, "Idempotency-Key": "browser-external-conflict"
            })
            self.assertEqual(status, 201)
            refresh_count = browser.evaluate("window.__availabilityStarted.length")
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=booking-error]')")
            self.assertEqual(browser.evaluate("window.__requests.at(-1).errorCode"), "table_unavailable")
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=booking-form]')"))
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=booking-party-size]').value"), "6")
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=confirmation]')"))
            browser.wait("document.querySelector('[data-testid=\"slot-t1+t2-18:00\"]') === null")
            browser.wait(f"window.__availabilityStarted.length > {refresh_count}")
            browser.wait("document.querySelector('[data-testid=availability-grid]') !== null && "
                         "document.querySelector('#search-results').getAttribute('aria-busy') === 'false'")
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=booking-error]')"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-uncertain]')"))

            browser.evaluate("""
              document.querySelector('[data-testid=party-size-input]').value='2';
              document.querySelector('[data-testid=date-input]').value='2035-09-25';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=\"slot-t3-21:00\"]') !== null")
            browser.evaluate("document.querySelector('[data-testid=\"slot-t3-21:00\"]').click()")
            browser.evaluate("""
              const party = document.querySelector('[data-testid=booking-party-size]');
              party.value='3';
              party.dispatchEvent(new Event('input', {bubbles:true}));
              document.querySelector('[data-testid=booking-submit]').click();
            """)
            browser.wait("!!document.querySelector('[data-testid=booking-error]')")
            browser.wait("!document.querySelector('[data-testid=booking-submit]').disabled")
            self.assertEqual(browser.evaluate("window.__requests.at(-1).errorCode"), "party_exceeds_capacity")
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-uncertain]')"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=confirmation]')"))
            rejected_request = browser.evaluate("window.__requests.at(-1)")
            self.assertEqual(json.loads(rejected_request["body"])["party_size"], 3)
            updated_party = browser.evaluate("""(() => {
              const input = document.querySelector('[data-testid=booking-party-size]');
              input.value='2';
              input.dispatchEvent(new Event('input', {bubbles:true}));
              return {
                value:input.value,
                error:!!document.querySelector('[data-testid=booking-error]'),
                disabled:document.querySelector('[data-testid=booking-submit]').disabled
              };
            })()""")
            self.assertEqual(updated_party, {"value": "2", "error": False, "disabled": False})
            browser.evaluate("window.__faultMode='before'")
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=booking-uncertain]')")
            self.assertTrue(browser.evaluate(
                "document.querySelector('[data-testid=booking-uncertain]').textContent.trim().length > 0"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-error]')"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=confirmation]')"))
            first_attempt = browser.evaluate("window.__requests.at(-1)")
            self.assertNotEqual(first_attempt["key"], rejected_request["key"])
            self.assertEqual(json.loads(first_attempt["body"])["party_size"], 2)
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=confirmation-reference]')")
            single_reference = browser.evaluate("document.querySelector('[data-testid=confirmation-reference]').textContent")
            self.assertRegex(single_reference, r"^[A-Z0-9]{6,12}$")
            single_details = browser.evaluate("document.querySelector('[data-testid=confirmation-details]').textContent")
            self.assertIn("Example", single_details)
            self.assertIn("Table Window nook", single_details)
            self.assertIn("2035-09-25 at 21:00", single_details)
            requests = browser.evaluate("window.__requests.slice(-2)")
            self.assertEqual(len(requests), 2)
            self.assertEqual((requests[0]["key"], requests[0]["body"]),
                             (requests[1]["key"], requests[1]["body"]))
            self.assertEqual(json.loads(requests[0]["body"])["table_id"], "t3")
            self.assertIsNone(first_attempt.get("status"))
            self.assertEqual(requests[1].get("status"), 201)
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-uncertain]')"))
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("window.__requests.length >= 4 && !document.querySelector('[data-testid=booking-submit]').disabled")
            replayed_reference = browser.evaluate("document.querySelector('[data-testid=confirmation-reference]').textContent")
            replay = browser.evaluate("window.__requests.at(-1)")
            self.assertEqual(replayed_reference, single_reference)
            self.assertEqual((replay["key"], replay["body"]),
                             (first_attempt["key"], first_attempt["body"]))
            self.assertEqual(replay.get("status"), 200)
            self.assertEqual(browser.evaluate("""(async () => {
              const response = await fetch('/reservations', {
                headers: {Authorization: 'Bearer ' + JSON.parse(localStorage.getItem('tablekeeper-session')).token}
              });
              const data = await response.json();
              return data.reservations.filter(item => item.reference === %s).length;
            })()""" % json.dumps(single_reference)), 1)

            browser.evaluate("""
              document.querySelector('[data-testid=party-size-input]').value='6';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=\"slot-t1+t2-21:00\"]') !== null")
            browser.evaluate("document.querySelector('[data-testid=\"slot-t1+t2-21:00\"]').click()")
            browser.evaluate("window.__faultMode='after'; document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=booking-uncertain]')")
            self.assertTrue(browser.evaluate(
                "document.querySelector('[data-testid=booking-uncertain]').textContent.trim().length > 0"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-error]')"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=confirmation]')"))
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=confirmation-reference]')")
            pair_reference = browser.evaluate("document.querySelector('[data-testid=confirmation-reference]').textContent")
            requests = browser.evaluate("window.__requests.slice(-2)")
            self.assertEqual((requests[0]["key"], requests[0]["body"]),
                             (requests[1]["key"], requests[1]["body"]))
            self.assertEqual(json.loads(requests[0]["body"])["table_ids"], ["t1", "t2"])
            self.assertEqual(requests[0].get("status"), 201)
            self.assertEqual(requests[1].get("status"), 200)
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-uncertain]')"))
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=booking-error]')"))
            self.assertTrue(browser.evaluate("!!document.querySelector('[data-testid=confirmation-tables]')"))
            pair_labels = browser.evaluate("document.querySelector('[data-testid=confirmation-tables]').textContent")
            self.assertIn("Table 1", pair_labels)
            self.assertIn("Table 2", pair_labels)
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("window.__requests.length >= 7 && !document.querySelector('[data-testid=booking-submit]').disabled")
            pair_replay = browser.evaluate("window.__requests.at(-1)")
            self.assertEqual((pair_replay["key"], pair_replay["body"]),
                             (requests[1]["key"], requests[1]["body"]))
            self.assertEqual(pair_replay.get("status"), 200)
            self.assertEqual(browser.evaluate("document.querySelector('[data-testid=confirmation-reference]').textContent"),
                             pair_reference)

            browser.evaluate("""
              document.querySelector('[data-testid=party-size-input]').value='2';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=availability-grid] [data-option=\"opaque|single\"]') !== null")
            browser.evaluate("document.querySelector('[data-testid=availability-grid] [data-option=\"opaque|single\"]').click()")
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=confirmation-reference]')")
            opaque_single = browser.evaluate("window.__requests.at(-1)")
            self.assertEqual(json.loads(opaque_single["body"])["table_id"], "opaque|single")
            opaque_single_reference = browser.evaluate(
                "document.querySelector('[data-testid=confirmation-reference]').textContent")

            browser.evaluate("""
              document.querySelector('[data-testid=party-size-input]').value='3';
              document.querySelector('#search-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=availability-grid] [data-option=\"opaque|left+opaque|right\"]') !== null")
            browser.evaluate("document.querySelector('[data-testid=availability-grid] [data-option=\"opaque|left+opaque|right\"]').click()")
            browser.evaluate("document.querySelector('[data-testid=booking-submit]').click()")
            browser.wait("!!document.querySelector('[data-testid=confirmation-reference]')")
            opaque_pair = browser.evaluate("window.__requests.at(-1)")
            self.assertEqual(json.loads(opaque_pair["body"])["table_ids"],
                             ["opaque|left", "opaque|right"])
            opaque_pair_reference = browser.evaluate(
                "document.querySelector('[data-testid=confirmation-reference]').textContent")
            self.assertNotEqual(opaque_single_reference, opaque_pair_reference)

            browser.call("Page.navigate", {"url": base + "/lookup"})
            browser.wait("location.pathname === '/lookup' && !!document.querySelector('[data-testid=lookup-submit]')")
            browser.evaluate(f"""
              document.querySelector('[data-testid=lookup-reference-input]').value={json.dumps(pair_reference)};
              document.querySelector('#lookup-form').requestSubmit();
            """)
            browser.wait("document.querySelector('[data-testid=reservation-status]')?.textContent === 'confirmed'")
            self.assertIn("Table", browser.evaluate("document.querySelector('[data-testid=reservation-tables]').textContent"))
            browser.evaluate("document.querySelector('[data-testid=reservation-cancel-button]').click()")
            browser.wait("document.querySelector('[data-testid=reservation-status]')?.textContent === 'cancelled'")
            self.assertFalse(browser.evaluate("!!document.querySelector('[data-testid=reservation-cancel-button]')"))
            self.assertTrue(single_reference)
            self.assertTrue(browser.evaluate("document.documentElement.scrollWidth <= innerWidth"))
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
