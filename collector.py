#!/usr/bin/env python3
"""
cute-claude-monitor — cross-platform Claude Code usage collector.

Scans ~/.claude/projects/**/*.jsonl incrementally, aggregates token usage into
hourly buckets, and writes a small JSON snapshot that the Windows widget reads.

Three usage windows are tracked, mirroring Claude Code's /usage screen:
  session  — the rolling 5-hour block
  week     — the weekly window, all models
  fable    — the weekly window, Fable/Mythos-tier models only

Only the Python standard library is used.

Usage:
    python3 collector.py --once           # single pass
    python3 collector.py --loop           # run forever (interval from config)
    python3 collector.py --rebuild        # discard cached state and rescan
    python3 collector.py --calibrate      # show totals for comparison with /usage
    python3 collector.py --calibrate --session 21 --week 58 --fable 4
                                          # derive real limits and save them
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"


def _runtime_data_dir() -> Path:
    """Keep mutable data outside a one-file executable's temporary bundle."""
    override = os.environ.get("CUTE_CLAUDE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support"
        else:
            root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return root / "CuteClaudeMonitor"
    return SOURCE_DIR


APP_DIR = _runtime_data_dir()
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

HOUR = 3600
DAY = 86400
WEEK = 7 * DAY
STATE_VERSION = 4

# --------------------------------------------------------------------------
# Pricing, USD per 1M tokens (base input, output).
# Source: bundled claude-api skill model table, cached 2026-06-24.
# Cache multipliers on base input: 5m write 1.25x, 1h write 2.0x, read 0.1x.
# --------------------------------------------------------------------------
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-mythos-preview": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),          # intro pricing applied below
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FAST_PRICING = {
    "claude-opus-5": (10.0, 50.0),
    "claude-opus-4-8": (10.0, 50.0),
}
SONNET5_INTRO = (2.0, 10.0)
SONNET5_INTRO_UNTIL = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
FALLBACK_PRICE = (5.0, 25.0)  # assumed Opus-tier when the model is unknown

DATE_SUFFIX = re.compile(r"-\d{8}$")

# --------------------------------------------------------------------------
# Live sync.  This is the same endpoint Claude Code's own /usage screen calls,
# authenticated with the OAuth token Claude Code already stores on this machine.
# It returns the authoritative server-side percentages, so no calibration and no
# token-weighting guesswork is involved.
# --------------------------------------------------------------------------
LIVE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
OAUTH_BETA = "oauth-2025-04-20"
# The payload's "limits" array is the authoritative shape -- an entry per bar on
# the /usage screen, with kind/percent/resets_at and, for the model-scoped one,
# the tier's display name. The flat top-level keys are a legacy fallback and are
# not always populated (seven_day_opus reads null on a Max plan even though the
# Fable bar is shown), so the array is tried first.
LIVE_KINDS = {"session": "session", "week": "weekly_all", "fable": "weekly_scoped"}
LIVE_LEGACY_KEYS = {"session": "five_hour", "week": "seven_day", "fable": "seven_day_opus"}

DEFAULT_CONFIG = {
    "output_path": "auto",
    "live_sync": True,
    "live_interval_seconds": 60,
    "interval_seconds": 5,
    "block_hours": 5,
    "budget_metric": "total",
    "retention_days": 32,
    "idle_after_minutes": 20,
    "limits": {"session": "auto", "week": "auto", "fable": "auto"},
    "week_anchor": None,       # ISO timestamp of a known weekly reset, or null
    "fable_prefixes": ["claude-fable-", "claude-mythos-"],
    "auto_floor": {"session": 2000000, "week": 20000000, "fable": 2000000},
}

NFIELDS = 6  # input, output, cw5m, cw1h, cache_read, requests


def log(msg: str) -> None:
    # PyInstaller's windowed mode has no stderr stream.
    if sys.stderr is not None:
        print(f"[collector] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            for key, value in user.items():
                if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                    cfg[key].update(value)
                else:
                    cfg[key] = value
        except Exception as exc:
            log(f"config.json unreadable ({exc}); using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    write_json_atomic(CONFIG_PATH, cfg, indent=2)


def windows_profile_from_interop() -> Path | None:
    """Ask Windows for %USERPROFILE% and translate it to a WSL path."""
    import subprocess

    try:
        raw = subprocess.run(
            ["cmd.exe", "/c", "echo %USERPROFILE%"],
            capture_output=True, text=True, timeout=15,
            cwd="/",  # avoids the "UNC paths are not supported" warning
        ).stdout.strip()
        if not raw or "%" in raw:
            return None
        translated = subprocess.run(
            ["wslpath", "-u", raw], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        return Path(translated) if translated else None
    except (OSError, subprocess.SubprocessError):
        return None


def windows_profile_candidates() -> list[Path]:
    users_dir = Path("/mnt/c/Users")
    if not users_dir.is_dir():
        return []
    skip = {"All Users", "Default", "Default User", "Public", "desktop.ini"}
    candidates: list[tuple[float, str]] = []
    try:
        entries = list(users_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name in skip:
            continue
        try:
            if entry.is_dir():
                candidates.append((entry.stat().st_mtime, entry.name))
        except OSError:
            continue  # locked or permission-denied profiles
    candidates.sort(reverse=True)
    return [users_dir / name for _, name in candidates]


def is_writable_dir(path: Path) -> bool:
    """Probe by actually writing — os.access lies on DrvFs."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_output_path(cfg: dict) -> Path:
    configured = cfg.get("output_path", "auto")
    if configured and configured != "auto":
        return Path(configured).expanduser()

    # Native Windows: the widget and collector share the same user profile, so
    # there is no path translation or profile scan to perform.
    if IS_WINDOWS:
        profile = Path(os.environ.get("USERPROFILE", Path.home()))
        target = profile / ".claude-widget"
        if is_writable_dir(target):
            return target / "usage.json"

    ordered: list[Path] = []
    # Under WSL, discover the matching Windows profile. Other Unix-like hosts
    # skip these probes and use their normal home directory below.
    if Path("/mnt/c/Users").is_dir():
        from_interop = windows_profile_from_interop()
        if from_interop is not None:
            ordered.append(from_interop)
        ordered.extend(p for p in windows_profile_candidates() if p != from_interop)
    for profile in ordered:
        target = profile / ".claude-widget"
        if is_writable_dir(target):
            return target / "usage.json"

    if ordered:
        log("no writable Windows profile found; falling back to ~/.claude-widget")
    return Path.home() / ".claude-widget" / "usage.json"


_native_loop_lock = None


def acquire_native_loop_lock() -> bool:
    """Prevent duplicate native Windows collectors from rewriting one state file."""
    global _native_loop_lock
    if not IS_WINDOWS:
        return True

    import msvcrt

    APP_DIR.mkdir(parents=True, exist_ok=True)
    handle = (APP_DIR / ".collector.lock").open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return False
    _native_loop_lock = handle
    return True


def new_state() -> dict:
    return {
        "version": STATE_VERSION,
        "files": {},         # path -> [size, mtime, offset]
        "hours": {},         # "<epoch_hour>" -> {"<model>|<speed>": [6 ints]}
        "recent": [],        # [ts, key, in, out, cw5, cw1h, cr]
        "seen": {},          # requestId -> ts (dedupe window)
        "days": {},          # "YYYY-MM-DD" -> [sessionId, ...]
        "limit_events": [],  # timestamps of observed 429s
        # Claude Code's session window: 5 hours from the first message, then a
        # new one begins on the first message after that expires. Tracked as a
        # running chain so it survives across passes.
        "session_start": None,
        "session_models": {},
        # Sanitized account percentages from the last successful live poll.
        # OAuth credentials and the raw response are never persisted.
        "last_live": None,
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            if state.get("version") == STATE_VERSION:
                state.setdefault("limit_events", [])
                state.setdefault("last_live", None)
                return state
            log("state schema changed; rebuilding")
        except Exception as exc:
            log(f"state.json unreadable ({exc}); rebuilding")
    return new_state()


def save_state(state: dict) -> None:
    # State contains transcript paths and usage history. It never contains the
    # OAuth token, but it is still private user data.
    write_json_atomic(STATE_PATH, state, mode=0o600)


def write_json_atomic(path: Path, payload: dict, indent: int | None = None,
                      mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if indent:
        serialized = json.dumps(payload, indent=indent)
    else:
        serialized = json.dumps(payload, separators=(",", ":"))
    tmp.write_text(serialized)
    if mode is not None:
        tmp.chmod(mode)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        # DrvFs (/mnt/c) can refuse rename-over in rare cases; fall back.
        # It can also complete the rename and still report an error, leaving no
        # temp file. In that case, accept the write only after verifying it.
        if not tmp.exists():
            try:
                if path.read_text() == serialized:
                    return
            except OSError:
                pass
            raise exc
        path.write_text(serialized)
        if mode is not None:
            path.chmod(mode)
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# transcript parsing
# --------------------------------------------------------------------------

def parse_timestamp(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def normalize_model(raw: str) -> str:
    model = (raw or "unknown").strip()
    # Claude Code surfaces context variants as "claude-opus-5[1m]".
    if "[" in model:
        model = model.split("[", 1)[0]
    # Dated snapshots: claude-haiku-4-5-20251001 -> claude-haiku-4-5
    model = DATE_SUFFIX.sub("", model)
    return model


def extract_usage(record: dict):
    """Return (request_id, ts, key, [in, out, cw5, cw1h, cr], session_id) or None."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message") or {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    request_id = record.get("requestId") or message.get("id")
    if not request_id:
        return None

    ts = parse_timestamp(record.get("timestamp", ""))
    if ts is None:
        return None

    model = normalize_model(message.get("model"))
    if model.startswith("<"):
        return None  # "<synthetic>" placeholders are not real API calls

    speed = usage.get("speed") or "standard"

    creation = usage.get("cache_creation") or {}
    cw5 = int(creation.get("ephemeral_5m_input_tokens") or 0)
    cw1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    if cw5 == 0 and cw1h == 0:
        # Older transcripts only carry the aggregate; assume the 5m tier.
        cw5 = int(usage.get("cache_creation_input_tokens") or 0)

    counts = [
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        cw5,
        cw1h,
        int(usage.get("cache_read_input_tokens") or 0),
    ]
    return request_id, ts, f"{model}|{speed}", counts, record.get("sessionId")


def is_limit_event(raw: bytes, record: dict) -> bool:
    """A 429 recorded in the transcript — a real observed ceiling."""
    if record.get("apiErrorStatus") == 429:
        return True
    return record.get("error") == "rate_limit"


def scan(state: dict, cfg: dict, now: float) -> int:
    """Read new transcript bytes and fold them into state. Returns new entries."""
    if not PROJECTS_DIR.is_dir():
        log(f"{PROJECTS_DIR} not found — is Claude Code installed for this user?")
        return 0

    retention_cutoff = now - cfg["retention_days"] * DAY
    files = state["files"]
    hours = state["hours"]
    seen_persisted = state["seen"]
    limit_events = state["limit_events"]
    seen_run: set[str] = set()
    new_entries: list[tuple] = []

    for path in sorted(PROJECTS_DIR.rglob("*.jsonl")):
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            continue

        prev = files.get(key)
        if prev is None:
            if st.st_mtime < retention_cutoff:
                # Too old to matter — mark consumed without reading a byte.
                files[key] = [st.st_size, st.st_mtime, st.st_size]
                continue
            offset = 0
        else:
            prev_size, prev_mtime, offset = prev
            if st.st_size == prev_size and abs(st.st_mtime - prev_mtime) < 1e-6:
                continue
            if st.st_size < offset:
                offset = 0  # file was replaced/truncated; re-read

        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            continue

        last_newline = chunk.rfind(b"\n")
        if last_newline == -1:
            files[key] = [st.st_size, st.st_mtime, offset]
            continue
        consumable = chunk[: last_newline + 1]
        files[key] = [st.st_size, st.st_mtime, offset + last_newline + 1]

        for raw in consumable.split(b"\n"):
            if not raw:
                continue
            interesting_usage = b'"usage"' in raw
            interesting_limit = b"429" in raw or b'"rate_limit"' in raw
            if not (interesting_usage or interesting_limit):
                continue
            try:
                record = json.loads(raw)
            except Exception:
                continue

            if interesting_limit and is_limit_event(raw, record):
                ts = parse_timestamp(record.get("timestamp", ""))
                if ts and ts >= retention_cutoff and ts not in limit_events:
                    limit_events.append(ts)

            if not interesting_usage:
                continue
            parsed = extract_usage(record)
            if parsed is None:
                continue
            request_id, ts, model_key, counts, session_id = parsed

            # Claude Code writes the same requestId more than once per turn.
            if request_id in seen_run or request_id in seen_persisted:
                continue
            seen_run.add(request_id)
            if ts >= now - 2 * DAY:
                seen_persisted[request_id] = ts

            if ts < retention_cutoff:
                continue

            new_entries.append((ts, model_key, counts, session_id))

    # The session chain is order-dependent, and entries arrive interleaved
    # across transcripts, so fold them in timestamp order rather than file order.
    new_entries.sort(key=lambda e: e[0])
    block_seconds = int(cfg["block_hours"] * HOUR)

    for ts, model_key, counts, session_id in new_entries:
        bucket = hours.setdefault(str(int(ts // HOUR) * HOUR), {})
        agg = bucket.get(model_key)
        if agg is None:
            bucket[model_key] = counts + [1]
        else:
            for i in range(5):
                agg[i] += counts[i]
            agg[5] += 1

        state["recent"].append([ts, model_key, *counts])

        # Roll the session window when this message lands after the previous
        # window expired. Anchored to the message timestamp, not the hour.
        start = state.get("session_start")
        if start is None or ts >= start + block_seconds:
            state["session_start"] = ts
            state["session_models"] = {}
        add_into(state["session_models"], model_key, counts + [1])

        if session_id:
            day = datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d")
            sessions = state["days"].setdefault(day, [])
            if session_id not in sessions:
                sessions.append(session_id)

    return len(new_entries)


def prune(state: dict, cfg: dict, now: float) -> None:
    retention_cutoff = now - cfg["retention_days"] * DAY
    state["hours"] = {h: v for h, v in state["hours"].items() if int(h) >= retention_cutoff}
    state["recent"] = sorted(
        (e for e in state["recent"] if e[0] >= now - 8 * HOUR), key=lambda e: e[0]
    )
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= now - 2 * DAY}
    state["limit_events"] = sorted(t for t in set(state["limit_events"]) if t >= retention_cutoff)

    keep_days = {
        datetime.fromtimestamp(now - i * DAY, timezone.utc).astimezone().strftime("%Y-%m-%d")
        for i in range(9)
    }
    state["days"] = {d: s for d, s in state["days"].items() if d in keep_days}
    state["files"] = {p: v for p, v in state["files"].items() if Path(p).exists()}


# --------------------------------------------------------------------------
# live sync
# --------------------------------------------------------------------------

_live_cache = {
    "attempt_at": 0.0,
    "success_at": 0.0,
    "data": None,
    "error": None,
}


class _NoAuthenticatedRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the bearer credential through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Ignore proxy environment variables for this credentialed request and reject
# every redirect. The usage endpoint is a fixed HTTPS origin; if it moves, live
# sync should fail closed until the new destination is reviewed explicitly.
_LIVE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoAuthenticatedRedirects(),
)


def fetch_live_usage(cfg: dict, now: float) -> tuple[dict | None, str | None]:
    """
    Ask Anthropic for the real numbers behind /usage.

    The OAuth token is read straight from Claude Code's own credential file and
    sent only to api.anthropic.com -- the same place Claude Code sends it. It is
    never logged or written anywhere. We never write to the credential file
    either: refreshing is Claude Code's job, and a bad write there would break
    your login. An expired token simply falls back to local estimates.
    """
    if not cfg.get("live_sync", True):
        return None, None

    interval = max(float(cfg.get("live_interval_seconds", 60)), 15.0)
    since_attempt = now - _live_cache["attempt_at"]
    retry_after = interval if _live_cache["data"] is not None else 15.0
    if since_attempt < retry_after:
        # Keep the last good payload for replacement after recovery, but never
        # present it as fresh account data after a failed refresh.
        if _live_cache["error"]:
            return None, _live_cache["error"]
        return _live_cache["data"], None

    _live_cache["attempt_at"] = now
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
    except OSError:
        _live_cache["error"] = "no credentials file"
        return None, _live_cache["error"]
    except ValueError:
        _live_cache["error"] = "credentials unreadable"
        return None, _live_cache["error"]

    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        _live_cache["error"] = "not signed in with OAuth"
        return None, _live_cache["error"]

    expires_at = oauth.get("expiresAt")
    if expires_at and float(expires_at) / 1000.0 < now:
        _live_cache["error"] = "token expired - run any Claude Code command to refresh"
        return None, _live_cache["error"]

    request = urllib.request.Request(
        LIVE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "User-Agent": "cute-claude-monitor",
        },
    )
    try:
        with _LIVE_OPENER.open(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _live_cache["error"] = f"HTTP {exc.code}"
        return None, _live_cache["error"]
    except Exception as exc:
        _live_cache["error"] = type(exc).__name__
        return None, _live_cache["error"]

    _live_cache["data"] = payload
    _live_cache["error"] = None
    _live_cache["success_at"] = now
    return payload, None


def _resets_in(resets_at, now: float) -> float | None:
    if not resets_at:
        return None
    try:
        if isinstance(resets_at, (int, float)):
            target = float(resets_at)
            if target > 1e11:      # milliseconds
                target /= 1000.0
        else:
            target = parse_timestamp(str(resets_at))
        return max(target - now, 0) if target else None
    except (TypeError, ValueError):
        return None


def live_window(payload: dict | None, gauge_id: str, now: float) -> dict | None:
    """Pull the live percentage, reset time and label for one gauge."""
    if not payload:
        return None

    entries = payload.get("limits")
    if isinstance(entries, list):
        wanted = LIVE_KINDS.get(gauge_id)
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("kind") != wanted:
                continue
            percent = entry.get("percent")
            if percent is None:
                continue
            label = None
            scope = entry.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict) and model.get("display_name"):
                    label = str(model["display_name"]).lower()
            return {
                "pct": float(percent) / 100.0,
                "resets_in": _resets_in(entry.get("resets_at"), now),
                "label": label,
                "severity": entry.get("severity"),
            }

    block = payload.get(LIVE_LEGACY_KEYS.get(gauge_id, ""))
    if isinstance(block, dict) and block.get("utilization") is not None:
        return {
            "pct": float(block["utilization"]) / 100.0,
            "resets_in": _resets_in(block.get("resets_at"), now),
            "label": None,
            "severity": None,
        }
    return None


def remember_live_usage(state: dict, payload: dict, now: float,
                        success_at: float) -> None:
    """Persist only the non-sensitive fields needed for a stale live view."""
    previous = state.get("last_live") or {}
    try:
        previous_at = float(previous.get("at") or 0)
    except (AttributeError, TypeError, ValueError):
        previous_at = 0.0
    if previous_at >= success_at:
        return

    gauges = {}
    for name in LIVE_KINDS:
        served = live_window(payload, name, now)
        if served is None:
            continue
        resets_in = served.get("resets_in")
        gauges[name] = {
            "pct": served["pct"],
            "resets_at": now + resets_in if resets_in is not None else None,
            "label": served.get("label"),
            "severity": served.get("severity"),
        }

    if gauges:
        state["last_live"] = {
            "at": success_at,
            "plan": payload.get("subscription_type") or payload.get("plan"),
            "gauges": gauges,
        }


def cached_live_window(state: dict, gauge_id: str, now: float) -> dict | None:
    """Return a last-known exact reading while it still belongs to this cycle."""
    cached = state.get("last_live") or {}
    try:
        age = max(now - float(cached["at"]), 0.0)
        item = (cached.get("gauges") or {}).get(gauge_id)
        if not isinstance(item, dict):
            return None

        resets_at = item.get("resets_at")
        if resets_at is not None:
            resets_in = float(resets_at) - now
            if resets_in <= 0:
                return None  # never carry a percentage into its next cycle
        else:
            if age > DAY:
                return None  # unbounded stale readings expire after one day
            resets_in = None

        return {
            "pct": float(item["pct"]),
            "resets_in": resets_in,
            "label": item.get("label"),
            "severity": item.get("severity"),
        }
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# aggregation helpers
# --------------------------------------------------------------------------

def add_into(target: dict[str, list[int]], key: str, counts: list[int]) -> None:
    agg = target.get(key)
    if agg is None:
        target[key] = list(counts)
    else:
        for i in range(NFIELDS):
            agg[i] += counts[i]


def totals_of(per_model: dict[str, list[int]]) -> list[int]:
    out = [0] * NFIELDS
    for agg in per_model.values():
        for i in range(NFIELDS):
            out[i] += agg[i]
    return out


def price_for(model_key: str, now: float) -> tuple[tuple[float, float], bool]:
    model, _, speed = model_key.partition("|")
    if speed == "fast" and model in FAST_PRICING:
        return FAST_PRICING[model], True
    if model == "claude-sonnet-5" and now < SONNET5_INTRO_UNTIL:
        return SONNET5_INTRO, True
    if model in PRICING:
        return PRICING[model], True
    return FALLBACK_PRICE, False


def cost_of(per_model: dict[str, list[int]], now: float) -> tuple[float, bool]:
    total = 0.0
    exact = True
    for model_key, agg in per_model.items():
        (pin, pout), known = price_for(model_key, now)
        exact = exact and known
        inp, out, cw5, cw1h, cr = agg[0], agg[1], agg[2], agg[3], agg[4]
        total += (
            inp * pin + out * pout
            + cw5 * pin * 1.25 + cw1h * pin * 2.0 + cr * pin * 0.1
        ) / 1_000_000
    return total, exact


def metric_of(agg: list[int], metric: str) -> int:
    inp, out, cw5, cw1h, cr = agg[0], agg[1], agg[2], agg[3], agg[4]
    if metric == "billable":
        return inp + out + cw5 + cw1h
    if metric == "output":
        return out
    return inp + out + cw5 + cw1h + cr  # "total"


def is_fable(model_key: str, cfg: dict) -> bool:
    model = model_key.partition("|")[0]
    return any(model.startswith(p) for p in cfg["fable_prefixes"])


def window_totals(hours: dict, start: float, end: float, cfg: dict,
                  fable_only: bool = False) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for h, per_model in hours.items():
        hour_start = int(h)
        if start <= hour_start < end:
            for key, agg in per_model.items():
                if fable_only and not is_fable(key, cfg):
                    continue
                add_into(out, key, agg)
    return out


def build_blocks(hours: dict, block_seconds: int) -> list[dict]:
    """Group hourly buckets into Claude Code-style rolling blocks."""
    ordered = sorted((int(h), v) for h, v in hours.items())
    blocks: list[dict] = []
    current: dict | None = None
    for hour_start, per_model in ordered:
        if (
            current is None
            or hour_start - current["start"] >= block_seconds
            or hour_start - current["last"] >= block_seconds
        ):
            current = {"start": hour_start, "last": hour_start, "models": {}}
            blocks.append(current)
        current["last"] = hour_start
        for key, agg in per_model.items():
            add_into(current["models"], key, agg)
    return blocks


def week_window(now: float, cfg: dict) -> tuple[float, float, float | None]:
    """Return (start, end, seconds_until_reset). Rolling if no anchor is set."""
    anchor_raw = cfg.get("week_anchor")
    if not anchor_raw:
        return now - WEEK, now + HOUR, None
    anchor = parse_timestamp(anchor_raw) if isinstance(anchor_raw, str) else float(anchor_raw)
    if anchor is None:
        return now - WEEK, now + HOUR, None
    periods = (now - anchor) // WEEK
    start = anchor + periods * WEEK
    end = start + WEEK
    return start, end, end - now


def day_bounds(now: float) -> tuple[float, float]:
    # Keep these datetimes naive/local so timestamp() resolves the UTC offset
    # independently at each boundary (an aware datetime returned by
    # astimezone() can carry only the current fixed offset on some platforms).
    local = datetime.fromtimestamp(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # A local day can be 23 or 25 hours across a daylight-saving transition.
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.timestamp(), tomorrow.timestamp()


def humanize(n: float) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


# --------------------------------------------------------------------------
# limit estimation
# --------------------------------------------------------------------------

# Note on 429s: transcripts record rate-limit errors, and it is tempting to
# treat the block total at each one as an observed ceiling. It does not work.
# Short-term (per-minute) rate limits and 5-hour usage limits both surface as
# a bare 429 with the same generic message, and the short-term ones fire early
# in a block when the running total is still small. Calibrating on them put the
# session limit at 15.6M against 45.7M actually used. So 429s are reported as an
# informational count only; real numbers come from `--calibrate`.


def peak_rolling_window(hours: dict, now: float, cfg: dict, span: float,
                        metric: str, fable_only: bool = False, samples: int = 24) -> int:
    """Largest total seen in any `span`-length window over recent history."""
    peak = 0
    for i in range(samples):
        end = now - i * DAY
        start = end - span
        totals = totals_of(window_totals(hours, start, end, cfg, fable_only))
        peak = max(peak, metric_of(totals, metric))
    return peak


def resolve_limit(cfg: dict, state: dict, name: str, auto_value: int) -> tuple[int, str]:
    configured = (cfg.get("limits") or {}).get(name, "auto")
    if isinstance(configured, (int, float)) and configured > 0:
        return int(configured), "configured"
    # Limit implied by the last live reading, kept fresh so the offline
    # fallback reflects current conditions rather than a stale calibration.
    implied = (state.get("implied_limits") or {}).get(name)
    if implied:
        return int(implied), "configured"
    floor = (cfg.get("auto_floor") or {}).get(name, 1_000_000)
    return max(int(auto_value), int(floor)), "estimated"


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def compute_windows(state: dict, cfg: dict, now: float) -> dict:
    """Shared by the snapshot and the --calibrate helper."""
    metric = cfg["budget_metric"]
    block_seconds = int(cfg["block_hours"] * HOUR)
    hours = state["hours"]
    blocks = build_blocks(hours, block_seconds)

    # The live session comes from the running chain, not from hourly blocks.
    # It stays the current window until it expires, whether or not you keep
    # messaging; a new one only opens on the next message after that.
    chain_start = state.get("session_start")
    if chain_start is not None and now < chain_start + block_seconds:
        session_models = state.get("session_models") or {}
        session_start = float(chain_start)
        session_end = session_start + block_seconds
        session_resets_in = max(session_end - now, 0)
    else:
        session_models = {}
        session_start = None
        session_end = None
        session_resets_in = None   # no window open; starts on your next message

    wk_start, wk_end, wk_resets = week_window(now, cfg)
    week_models = window_totals(hours, wk_start, wk_end, cfg)
    fable_models = window_totals(hours, wk_start, wk_end, cfg, fable_only=True)

    return {
        "metric": metric,
        "block_seconds": block_seconds,
        "blocks": blocks,
        "session": {
            "models": session_models,
            "start": session_start,
            "end": session_end,
            "resets_in": session_resets_in,
        },
        "week": {
            "models": week_models,
            "start": wk_start,
            "end": wk_end,
            "resets_in": wk_resets,
        },
        "fable": {
            "models": fable_models,
            "start": wk_start,
            "end": wk_end,
            "resets_in": wk_resets,
        },
    }


def build_snapshot(state: dict, cfg: dict, now: float) -> dict:
    w = compute_windows(state, cfg, now)
    metric = w["metric"]
    hours = state["hours"]

    # ---- limits ----------------------------------------------------------
    completed = [
        metric_of(totals_of(b["models"]), metric)
        for b in w["blocks"]
        if b["start"] + w["block_seconds"] <= now and b["start"] >= now - 30 * DAY
    ]
    auto_session = max(completed, default=0)

    auto_week = peak_rolling_window(hours, now, cfg, WEEK, metric)
    auto_fable = peak_rolling_window(hours, now, cfg, WEEK, metric, fable_only=True)

    live, live_error = fetch_live_usage(cfg, now)
    if live is not None:
        remember_live_usage(
            state, live, now, float(_live_cache["success_at"] or now)
        )
    use_cached_live = (
        live is None
        and bool(live_error)
        and cfg.get("live_sync", True)
    )
    cached_live_used = False

    gauges = []
    for name, auto_value, label in (
        ("session", auto_session, "session"),
        ("week", auto_week, "week"),
        ("fable", auto_fable, "fable"),
    ):
        window = w[name]
        totals = totals_of(window["models"])
        used = metric_of(totals, metric)
        limit, source = resolve_limit(cfg, state, name, auto_value)
        cost, cost_exact = cost_of(window["models"], now)
        pct = (used / limit) if limit else 0.0
        resets_in = window["resets_in"]
        detail = "calibrated" if source == "configured" else "your busiest so far"

        # Server-side numbers win outright: they are what the account is
        # actually measured against, so no calibration or weighting is involved.
        severity = None
        served = live_window(live, name, now)
        served_from_cache = False
        if served is None and use_cached_live:
            served = cached_live_window(state, name, now)
            served_from_cache = served is not None
        if served is not None:
            pct = served["pct"]
            source = "last_live" if served_from_cache else "live"
            detail = "last successful account sync" if served_from_cache else "live from your account"
            cached_live_used = cached_live_used or served_from_cache
            severity = served.get("severity")
            if served.get("resets_in") is not None:
                resets_in = served["resets_in"]
            if served.get("label"):
                label = served["label"]     # e.g. the tier is renamed server-side
            # Re-derive what the limit would be in our own metric, so if live
            # sync ever drops out the fallback is calibrated to right now
            # instead of to whenever someone last ran --calibrate.
            if not served_from_cache and pct > 0.05 and used > 0:
                state.setdefault("implied_limits", {})[name] = int(used / pct)

        gauges.append({
            "id": name,
            "label": label,
            "severity": severity,
            "used": used,
            "limit": limit,
            "pct": pct,
            "resets_in": resets_in,
            "source": source,
            "detail": detail,
            "cost_usd": round(cost, 4),
            "cost_exact": cost_exact,
            "used_label": humanize(used),
            "limit_label": humanize(limit),
        })

    # ---- burn rate (last 30 min of raw events) ---------------------------
    burn_window = 30 * 60
    burn_tokens = 0
    for entry in state["recent"]:
        if entry[0] >= now - burn_window:
            burn_tokens += metric_of(list(entry[2:]) + [1], metric)
    tokens_per_min = burn_tokens / (burn_window / 60)

    last_activity = state["recent"][-1][0] if state["recent"] else None
    if last_activity is None:
        last_activity = max((int(h) for h in hours), default=None)
    idle_seconds = (now - last_activity) if last_activity else None
    status = "idle" if (idle_seconds is None
                        or idle_seconds > cfg["idle_after_minutes"] * 60) else "active"

    # ---- today -----------------------------------------------------------
    today_start, today_end = day_bounds(now)
    today_models = window_totals(hours, today_start, today_end, cfg)
    today_totals = totals_of(today_models)
    today_cost, today_cost_exact = cost_of(today_models, now)
    today_key = datetime.fromtimestamp(now, timezone.utc).astimezone().strftime("%Y-%m-%d")

    # ---- sparkline: last 24 hourly values --------------------------------
    current_hour = int(now // HOUR) * HOUR
    sparkline = []
    for i in range(23, -1, -1):
        bucket = hours.get(str(current_hour - i * HOUR), {})
        sparkline.append(metric_of(totals_of(bucket), metric) if bucket else 0)

    # ---- dominant model in the session ------------------------------------
    session_models = w["session"]["models"]
    model_rows = []
    for model_key, agg in sorted(session_models.items(),
                                 key=lambda kv: metric_of(kv[1], metric), reverse=True):
        model, _, speed = model_key.partition("|")
        model_rows.append({
            "model": model,
            "speed": speed,
            "requests": agg[5],
            "tokens": metric_of(agg, metric),
        })

    recent_limit_hits = [t for t in state.get("limit_events", []) if t >= now - 7 * DAY]

    cached = state.get("last_live") or {}
    if live is not None:
        live_age = max(now - float(_live_cache["success_at"] or now), 0.0)
        live_plan = live.get("subscription_type") or live.get("plan")
        last_success_at = float(_live_cache["success_at"] or now)
    elif cached_live_used:
        last_success_at = float(cached.get("at") or 0)
        live_age = max(now - last_success_at, 0.0)
        live_plan = cached.get("plan")
    else:
        live_age = None
        live_plan = None
        last_success_at = None

    return {
        "schema": 2,
        "generated_at": now,
        "metric": metric,
        "status": status,
        "idle_seconds": round(idle_seconds) if idle_seconds is not None else None,
        "gauges": gauges,
        "burn": {
            "tokens_per_min": round(tokens_per_min, 1),
            "window_minutes": burn_window // 60,
        },
        "models": model_rows,
        "today": {
            "tokens": metric_of(today_totals, metric),
            "tokens_label": humanize(metric_of(today_totals, metric)),
            "cost_usd": round(today_cost, 4),
            "cost_exact": today_cost_exact,
            "sessions": len(state["days"].get(today_key, [])),
        },
        "sparkline": sparkline,
        "limit_hits_7d": len(recent_limit_hits),
        "week_anchored": bool(cfg.get("week_anchor")),
        "live": {
            "ok": live is not None,
            "cached": cached_live_used,
            "error": live_error,
            "plan": live_plan,
            "age_seconds": round(live_age, 1) if live_age is not None else None,
            "last_success_at": last_success_at,
        },
    }


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

_last_state_save = 0.0


def run_once(state: dict, cfg: dict, output_path: Path) -> dict:
    global _last_state_save
    now = time.time()
    added = scan(state, cfg, now)
    prune(state, cfg, now)
    snapshot = build_snapshot(state, cfg, now)
    write_json_atomic(output_path, snapshot)
    # state.json is ~0.4 MB; rewriting it every tick would be pointless churn.
    if added or now - _last_state_save > 60:
        save_state(state)
        _last_state_save = now
    return snapshot


def do_calibrate(state: dict, cfg: dict, args) -> int:
    """Turn '/usage says N%' into real token limits."""
    now = time.time()
    scan(state, cfg, now)
    prune(state, cfg, now)
    save_state(state)
    w = compute_windows(state, cfg, now)
    metric = w["metric"]

    supplied = {"session": args.session, "week": args.week, "fable": args.fable}
    limits = dict(cfg.get("limits") or {})
    changed = False

    print("Current usage in each window (metric: %s)\n" % metric)
    for name in ("session", "week", "fable"):
        used = metric_of(totals_of(w[name]["models"]), metric)
        pct = supplied[name]
        line = f"  {name:8} {humanize(used):>9} tokens used"
        if pct:
            if pct <= 0:
                print(line + "   (skipped: percentage must be > 0)")
                continue
            derived = int(round(used / (pct / 100.0)))
            limits[name] = derived
            changed = True
            print(line + f"   = {pct:g}%  ->  limit {humanize(derived)} ({derived})")
        else:
            print(line)

    if changed:
        cfg["limits"] = limits
        save_config(cfg)
        print(f"\nSaved to {CONFIG_PATH}. Restart the collector to pick them up:")
        print("  systemctl --user restart claude-usage-collector")
    else:
        print("\nRun /usage in Claude Code, then re-run with the percentages it shows:")
        print("  python3 collector.py --calibrate --session 21 --week 58 --fable 4")
        print("\nCalibrate soon after a burst of usage — the bigger the numbers,")
        print("the less a rounded percentage skews the derived limit.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code usage collector")
    parser.add_argument("--once", action="store_true", help="single pass, then exit")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--rebuild", action="store_true", help="discard cached state first")
    parser.add_argument("--print", action="store_true", help="dump the snapshot to stdout")
    parser.add_argument("--calibrate", action="store_true",
                        help="derive real limits from /usage percentages")
    parser.add_argument("--session", type=float, help="session %% from /usage")
    parser.add_argument("--week", type=float, help="weekly %% from /usage")
    parser.add_argument("--fable", type=float, help="fable weekly %% from /usage")
    args = parser.parse_args()

    cfg = load_config()

    if args.loop and not acquire_native_loop_lock():
        log("another collector is already running")
        return 1

    if args.rebuild and STATE_PATH.exists():
        STATE_PATH.unlink()
        log("state discarded; rescanning")

    state = load_state()

    if args.calibrate:
        return do_calibrate(state, cfg, args)

    output_path = resolve_output_path(cfg)

    if args.loop:
        interval = max(float(cfg["interval_seconds"]), 1.0)
        log(f"writing {output_path} every {interval:g}s (ctrl-c to stop)")
        first = True
        while True:
            try:
                start = time.time()
                snapshot = run_once(state, cfg, output_path)
                if first:
                    g = {x["id"]: x for x in snapshot["gauges"]}
                    log("first pass in %.1fs — session %s/%s, week %s/%s, fable %s/%s" % (
                        time.time() - start,
                        g["session"]["used_label"], g["session"]["limit_label"],
                        g["week"]["used_label"], g["week"]["limit_label"],
                        g["fable"]["used_label"], g["fable"]["limit_label"]))
                    first = False
            except KeyboardInterrupt:
                log("stopped")
                return 0
            except Exception as exc:  # keep the daemon alive
                log(f"pass failed: {exc!r}")
            time.sleep(interval)

    snapshot = run_once(state, cfg, output_path)
    if args.print or not args.once:
        print(json.dumps(snapshot, indent=2))
    else:
        log(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
