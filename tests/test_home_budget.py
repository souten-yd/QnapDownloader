"""v0.1.33 request budget: API calls made by the page during the first 10 s on #/home.

Runs web/app.js (or $APP_JS, e.g. an older copy for a before/after comparison) in node against a
stub DOM and the real test Hub with a fake running engine; timers run on a virtual clock.
Before (v0.1.30 app.js): 7 requests. After: see HOME_REQUEST_BUDGET.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_webapp

ROOT = Path(__file__).resolve().parent.parent
HOME_REQUEST_BUDGET = 3          # session + first state call + the held state long-poll
OLD_REQUESTS = 7                 # measured with the v0.1.30 app.js (session, health, bookmarks, status,
                                 # settings, downloads, accounts)

RUNNING = {"phase": "running", "installed": True, "mode": "container", "running": True, "ready": True,
           "setup_running": False, "autostart": False, "preset_applied": True, "steps": [], "error": None,
           "components": [], "container": {"available": True, "state": "running", "name": "x"},
           "native_leftovers": {"present": False, "bytes": 0}, "can_migrate": False, "heap_mb": None,
           "core_revision": 1, "restart_count": 0, "preferred_mode": "container"}


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HomeRequestBudgetTests(test_webapp.HttpTests):
    # only the budget test below runs; HttpTests' own tests are inherited but disabled here
    def run_budget(self, app_js, hash="#/home"):
        if not self.csrf:
            self.login()
        folder = self.req("/api/settings/folder", {"path": str(self.allowed.resolve())})
        self.assertEqual(folder[0], 200)
        jd2 = {"connected": True, "can_update": True, "can_preset": True, "can_send": True,
               "revision": 1, "update_available": False}
        snap = {"packages": [{"id": "1", "name": "p", "running": True}], "links": [], "page": 0, "has_more": False}
        self.server.accounts = MagicMock()
        self.server.accounts.list.return_value = {"items": [{"id": "1"}], "truncated": False, "age_seconds": 0}
        cookie = "; ".join(c.name + "=" + c.value for c in self.cookie)
        with patch.object(self.server.engine, "status", return_value=dict(RUNNING)), \
                patch.object(self.server.jd2, "status", return_value=jd2), \
                patch.object(self.server.jd2, "downloads_snapshot", return_value=snap):
            out = subprocess.run(["node", str(ROOT / "tests" / "home_budget.js"), str(app_js), self.url, cookie, hash],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("error", out.stderr.lower(), out.stderr)
        self.assertNotIn("unhandled", out.stderr, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_every_view_and_settings_route_loads_without_script_errors(self):
        for hash in ("#/downloads", "#/links", "#/settings/jd2/accounts", "#/settings/browser",
                     "#/settings/connection/share", "#/settings/system", "#/settings/engine", "#/settings/https"):
            with self.subTest(hash=hash):
                result = self.run_budget(ROOT / "web" / "app.js", hash)
                self.assertLessEqual(result["count"], 12, result["requests"])

    def test_home_makes_at_most_the_budgeted_requests(self):
        result = self.run_budget(os.environ.get("APP_JS") or ROOT / "web" / "app.js")
        print("home requests:", result["count"], result["requests"])
        if not os.environ.get("APP_JS"):
            self.assertLessEqual(result["count"], HOME_REQUEST_BUDGET, result["requests"])
            self.assertLessEqual(result["count"], OLD_REQUESTS // 2)      # at least 50% fewer
            self.assertFalse([r for r in result["requests"] if "/api/engine/status" in r or "/api/rb/status" in r
                              or "/api/jd2/downloads" in r or "/api/accounts" in r])


# keep only the budget test from the inherited HttpTests suite
for _name in [n for n in dir(HomeRequestBudgetTests) if n.startswith("test_") and
              n not in ("test_home_makes_at_most_the_budgeted_requests", "test_every_view_and_settings_route_loads_without_script_errors")]:
    setattr(HomeRequestBudgetTests, _name, None)

if __name__ == "__main__":
    unittest.main()
