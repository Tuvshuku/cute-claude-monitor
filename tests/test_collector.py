import json
import os
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
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

    def test_invalid_live_payload_fails_closed(self):
        original_credentials = collector.CREDENTIALS_PATH
        original_cache = dict(collector._live_cache)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                credentials = Path(tmp) / "credentials.json"
                credentials.write_text(json.dumps({
                    "claudeAiOauth": {"accessToken": "test-token"},
                }))
                collector.CREDENTIALS_PATH = credentials
                collector._live_cache.update(
                    attempt_at=0.0, success_at=0.0, data=None, error=None
                )
                with patch.object(
                    collector._LIVE_OPENER, "open", return_value=_Response([])
                ):
                    self.assertEqual(
                        collector.fetch_live_usage(
                            {"live_sync": True, "live_interval_seconds": 60}, 1000
                        ),
                        (None, "invalid response"),
                    )
        finally:
            collector.CREDENTIALS_PATH = original_credentials
            collector._live_cache.clear()
            collector._live_cache.update(original_cache)

    def test_snapshot_prefers_valid_last_live_reading_over_local_fallback(self):
        # Keep the fixture inside Windows' reliably supported local-time range.
        now = 1_800_000_000.0
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


class InputHardeningTests(unittest.TestCase):
    def test_invalid_config_values_fall_back_to_safe_defaults(self):
        cfg = json.loads(json.dumps(collector.DEFAULT_CONFIG))
        cfg.update({
            "interval_seconds": "nan",
            "block_hours": -1,
            "retention_days": None,
            "idle_after_minutes": [],
            "budget_metric": "surprise",
            "week_anchor": {"bad": True},
            "fable_prefixes": [1, None],
            "limits": {"session": -5, "week": "oops", "fable": "auto"},
        })

        validated = collector.validate_config(cfg)

        self.assertEqual(validated["interval_seconds"], 5)
        self.assertEqual(validated["block_hours"], 5)
        self.assertEqual(validated["retention_days"], 32)
        self.assertEqual(validated["idle_after_minutes"], 20)
        self.assertEqual(validated["budget_metric"], "total")
        self.assertIsNone(validated["week_anchor"])
        self.assertEqual(validated["fable_prefixes"], collector.DEFAULT_CONFIG["fable_prefixes"])
        self.assertEqual(validated["limits"], collector.DEFAULT_CONFIG["limits"])

    def test_malformed_usage_fields_do_not_break_parsing(self):
        parsed = collector.extract_usage({
            "type": "assistant",
            "requestId": 123,
            "timestamp": 1_800_000_000,
            "sessionId": {"not": "hashable"},
            "message": {
                "model": ["unexpected"],
                "usage": {
                    "input_tokens": "not-a-number",
                    "output_tokens": -9,
                    "cache_creation": "not-an-object",
                    "cache_read_input_tokens": float("inf"),
                },
            },
        })

        self.assertIsNotNone(parsed)
        request_id, timestamp, _model, counts, session_id = parsed
        self.assertEqual(request_id, "123")
        self.assertEqual(timestamp, 1_800_000_000)
        self.assertEqual(counts, [0, 0, 0, 0, 0])
        self.assertIsNone(session_id)

    def test_scan_skips_bad_records_and_future_timestamps(self):
        now = 1_800_000_000.0
        valid_time = datetime.fromtimestamp(now - 60, timezone.utc).isoformat()
        future_time = datetime.fromtimestamp(now + 2 * collector.DAY, timezone.utc).isoformat()
        records = [
            {"message": ["usage"]},
            {
                "type": "assistant", "requestId": "valid", "timestamp": valid_time,
                "sessionId": "one", "message": {
                    "model": "claude-test", "usage": {"input_tokens": 10},
                },
            },
            {
                "type": "assistant", "requestId": "bad-count", "timestamp": valid_time,
                "message": {
                    "model": "claude-test", "usage": {"output_tokens": "broken"},
                },
            },
            {
                "type": "assistant", "requestId": "future", "timestamp": future_time,
                "message": {
                    "model": "claude-test", "usage": {"input_tokens": 999},
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            transcript = project_dir / "session.jsonl"
            transcript.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n"
            )
            os.utime(transcript, (now, now))
            with patch.object(collector, "PROJECTS_DIR", project_dir):
                state = collector.new_state()
                added = collector.scan(
                    state, json.loads(json.dumps(collector.DEFAULT_CONFIG)), now
                )

        self.assertEqual(added, 2)
        self.assertEqual(len(state["recent"]), 2)
        self.assertEqual(sum(entry[2] for entry in state["recent"]), 10)

    def test_invalid_state_structure_is_rebuilt(self):
        original_state_path = collector.STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "state.json"
                path.write_text(json.dumps({
                    "version": collector.STATE_VERSION,
                    "hours": [],
                }))
                collector.STATE_PATH = path
                state = collector.load_state()
                self.assertEqual(state["hours"], {})
                self.assertEqual(state["version"], collector.STATE_VERSION)
        finally:
            collector.STATE_PATH = original_state_path


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
