import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import collector


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class LiveUsageCacheTests(unittest.TestCase):
    def test_failed_refresh_does_not_serve_last_good_payload_as_fresh(self):
        original_credentials = collector.CREDENTIALS_PATH
        original_cache = dict(collector._live_cache)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                credentials = Path(tmp) / "credentials.json"
                credentials.write_text(json.dumps({
                    "claudeAiOauth": {"accessToken": "test-token"},
                }))
                collector.CREDENTIALS_PATH = credentials
                collector._live_cache.clear()
                collector._live_cache.update(
                    attempt_at=0.0, success_at=0.0, data=None, error=None
                )
                cfg = {"live_sync": True, "live_interval_seconds": 60}
                payload = {"limits": []}

                with patch.object(
                    collector._LIVE_OPENER,
                    "open",
                    return_value=_Response(payload),
                ) as request:
                    self.assertEqual(
                        collector.fetch_live_usage(cfg, 1000.0), (payload, None)
                    )
                    self.assertEqual(
                        collector.fetch_live_usage(cfg, 1010.0), (payload, None)
                    )
                    self.assertEqual(request.call_count, 1)

                with patch.object(
                    collector._LIVE_OPENER,
                    "open",
                    side_effect=urllib.error.URLError("offline"),
                ):
                    data, error = collector.fetch_live_usage(cfg, 1060.0)
                    self.assertIsNone(data)
                    self.assertEqual(error, "URLError")

                # The retry throttle should retain the error, not relabel the
                # older successful payload as an exact current reading.
                with patch.object(collector._LIVE_OPENER, "open") as request:
                    data, error = collector.fetch_live_usage(cfg, 1061.0)
                    self.assertIsNone(data)
                    self.assertEqual(error, "URLError")
                    request.assert_not_called()
        finally:
            collector.CREDENTIALS_PATH = original_credentials
            collector._live_cache.clear()
            collector._live_cache.update(original_cache)

    def test_last_live_reading_is_sanitized_and_expires_at_reset(self):
        state = collector.new_state()
        payload = {
            "subscription_type": "max",
            "unrelated": {"must_not_be_saved": True},
            "limits": [{
                "kind": "weekly_all",
                "percent": 73,
                "resets_at": 2000,
                "severity": "warning",
            }],
        }

        collector.remember_live_usage(state, payload, now=1000, success_at=990)

        self.assertEqual(state["last_live"]["at"], 990)
        self.assertEqual(state["last_live"]["plan"], "max")
        self.assertNotIn("unrelated", state["last_live"])
        cached = collector.cached_live_window(state, "week", now=1500)
        self.assertEqual(cached["pct"], 0.73)
        self.assertEqual(cached["resets_in"], 500)
        self.assertIsNone(collector.cached_live_window(state, "week", now=2000))

    def test_snapshot_prefers_valid_last_live_reading_over_local_fallback(self):
        now = 10_000.0
        state = collector.new_state()
        state["last_live"] = {
            "at": now - 120,
            "plan": "max",
            "gauges": {
                name: {
                    "pct": pct,
                    "resets_at": now + 3600,
                    "label": None,
                    "severity": None,
                }
                for name, pct in (("session", 0.31), ("week", 0.73), ("fable", 0.22))
            },
        }
        cfg = json.loads(json.dumps(collector.DEFAULT_CONFIG))

        with patch.object(
            collector, "fetch_live_usage", return_value=(None, "HTTP 429")
        ):
            snapshot = collector.build_snapshot(state, cfg, now)

        self.assertFalse(snapshot["live"]["ok"])
        self.assertTrue(snapshot["live"]["cached"])
        self.assertEqual(snapshot["live"]["age_seconds"], 120)
        self.assertEqual(
            [gauge["source"] for gauge in snapshot["gauges"]],
            ["last_live", "last_live", "last_live"],
        )
        self.assertEqual(snapshot["gauges"][1]["pct"], 0.73)


class DayBoundsTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone support")
    def test_local_day_bounds_follow_dst_transitions(self):
        original_tz = os.environ.get("TZ")
        try:
            # POSIX timezone form avoids depending on the host's zoneinfo files.
            os.environ["TZ"] = "EST5EDT,M3.2.0/2,M11.1.0/2"
            time.tzset()

            spring_noon = time.mktime((2024, 3, 10, 12, 0, 0, 0, 0, -1))
            start, end = collector.day_bounds(spring_noon)
            self.assertEqual(end - start, 23 * collector.HOUR)

            fall_noon = time.mktime((2024, 11, 3, 12, 0, 0, 0, 0, -1))
            start, end = collector.day_bounds(fall_noon)
            self.assertEqual(end - start, 25 * collector.HOUR)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()


class OutputPathTests(unittest.TestCase):
    def test_native_windows_auto_output_uses_user_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(collector, "IS_WINDOWS", True), patch.dict(
                os.environ, {"USERPROFILE": tmp}
            ):
                path = collector.resolve_output_path({"output_path": "auto"})
            self.assertEqual(path, Path(tmp) / ".claude-widget" / "usage.json")

    def test_configured_output_path_expands_home(self):
        path = collector.resolve_output_path({"output_path": "~/custom-usage.json"})
        self.assertEqual(path, Path.home() / "custom-usage.json")


class SecurityHardeningTests(unittest.TestCase):
    def test_authenticated_requests_never_follow_redirects(self):
        request = collector.urllib.request.Request(
            collector.LIVE_URL,
            headers={"Authorization": "Bearer secret-test-token"},
        )
        handler = collector._NoAuthenticatedRedirects()
        redirected = handler.redirect_request(
            request, None, 302, "Found", {}, "https://example.com/steal"
        )
        self.assertIsNone(redirected)

    def test_live_opener_ignores_environment_proxies(self):
        proxy_handlers = [
            handler
            for handler in collector._LIVE_OPENER.handlers
            if isinstance(handler, collector.urllib.request.ProxyHandler)
        ]
        # build_opener omits an explicitly empty ProxyHandler entirely; the
        # important property is that no environment-backed proxy handler exists.
        self.assertEqual(proxy_handlers, [])

    @unittest.skipIf(os.name == "nt", "Windows does not expose POSIX permission bits")
    def test_private_json_is_created_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            collector.write_json_atomic(path, {"safe": True}, mode=0o600)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
