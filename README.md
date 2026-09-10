# cute-claude-monitor

A small always-on-top Claude Code usage widget for the Windows desktop. The
collector can run directly on Windows or inside WSL. It tracks the same three
windows as `/usage`:
**session** (5-hour block), **week**, and **fable** (weekly, Fable/Mythos-tier only).

## Native Windows — easiest

Download `CuteClaudeMonitor.exe` from the repository's **Releases** page and
double-click it. It is a standalone app: Python and WSL are not required.

Native mode reads Claude Code data from `%USERPROFILE%\.claude`, so Claude Code
must be installed and logged in directly on Windows. Mutable state and the
example configuration live in `%LOCALAPPDATA%\CuteClaudeMonitor`; the widget
snapshot lives in `%USERPROFILE%\.claude-widget`.

The executable is currently unsigned, so Windows SmartScreen may show an
unrecognized-app warning on the first launch.

Run either native mode or WSL mode, not both: two collectors should not write
the same `%USERPROFILE%\.claude-widget\usage.json` file. To switch from an
existing WSL installation, first run
`systemctl --user disable --now claude-usage-collector` inside WSL.

### Run the source from Windows Terminal

With Python 3 installed:

```powershell
git clone https://github.com/Tuvshuku/cute-claude-monitor.git
cd cute-claude-monitor
powershell -ExecutionPolicy Bypass -File .\run-windows.ps1
```

You can also double-click `Start-Windows.vbs` to launch the source version with
no terminal window.

## WSL setup

Use this mode when Claude Code and its `.claude` data live inside WSL:

```
WSL (kali-linux)                         Windows
─────────────────────────────────        ──────────────────────────────
~/.claude/projects/**/*.jsonl
        │  incremental read
        ▼
   collector.py  ──writes──►  /mnt/c/Users/<you>/.claude-widget/usage.json
                                            │  read every 2s
                                            ▼
                                        widget.ps1  (WPF card)
```

The two WSL-mode halves only ever share one small JSON file, so there is no port, no
firewall rule, and no WSL networking to configure.

```bash
git clone https://github.com/Tuvshuku/cute-claude-monitor.git ~/cute.app
cd ~/cute.app
./install.sh                # copies widget.ps1 + Start-Widget.vbs to the Windows folder
./run-collector.sh          # leave running
```

The built-in defaults work without a configuration file. To customize them,
copy the example first:

```bash
cp config.example.json config.json
```

Then on Windows, open `C:\Users\<you>\.claude-widget\Start-Widget.vbs`.

To make both halves start on their own:

```bash
./install.sh --service --autostart
```

- `--service` registers a systemd **user** service (`claude-usage-collector`)
  so the collector starts with WSL. Logs: `journalctl --user -u claude-usage-collector -f`.
- `--autostart` drops the launcher into the Windows Startup folder.

Three things have to line up for the widget to be live right after you sign in,
and the installer configures and checks all of them:

1. **Windows starts the launcher** — the `.vbs` in the Startup folder.
2. **The launcher starts WSL** — it fires `wsl.exe -d <distro> -e true` before
   opening the widget. Without this, nothing would boot the distro and the
   collector would never run. `install.sh` bakes your distro name into the copy.
3. **WSL starts the collector** — the systemd user service, which needs
   `loginctl enable-linger <you>` so the user manager comes up at boot instead of
   waiting for an interactive shell. The installer enables this when possible
   and prints a command to run if the system rejects the automatic change.

WSL takes a few seconds to boot, so the card shows an `offline` badge briefly
before going live.

### After a reboot

Nothing to do — signing in to Windows restarts both halves. If the card doesn't
come back, run the health check; it prints the fix next to whatever failed:

```bash
cd ~/cute.app && ./status.sh
```

To start things by hand:

```bash
systemctl --user start claude-usage-collector     # collector
```

and on Windows, open `C:\Users\<you>\.claude-widget\Start-Widget.vbs`
(or press `Win+R` and paste `shell:startup` to reach the same launcher).

## Live sync — the numbers are exact

The percentages are **read from your account**, not estimated. The collector
calls `GET https://api.anthropic.com/api/oauth/usage` — the same endpoint Claude
Code's own `/usage` screen uses — so the three bars match it digit for digit,
including the reset times.

It authenticates with the OAuth token Claude Code already keeps in
`~/.claude/.credentials.json`. Ground rules the code sticks to:

- The token is sent **only** to `api.anthropic.com`, the same place Claude Code
  sends it. The request is a direct HTTPS `GET`; redirects and environment
  proxies are rejected so the bearer credential cannot be forwarded to another
  destination. It is never logged, printed, or written anywhere.
- The credential file is opened **read-only**. Refreshing the token is Claude
  Code's job; a bad write there would break your login.
- The request reads usage metadata only. It does not invoke a model, create
  input/output tokens, or use an API key.
- If the token has expired, live sync is skipped and the widget quietly falls
  back to local estimates. Running any Claude Code command refreshes it.
- Polled once a minute (`live_interval_seconds`), not on every 5s pass.
- Turn it off with `"live_sync": false` in `config.json`.

The response's `limits` array is the source of truth — one entry per bar, with
`kind` (`session` / `weekly_all` / `weekly_scoped`), `percent`, `resets_at`, and
for the scoped one the tier's display name, which the widget uses as the gauge
label. The flat `five_hour` / `seven_day` / `seven_day_opus` keys are only a
fallback: on a Max plan `seven_day_opus` reads `null` even though the Fable bar
is shown, so relying on it would silently drop that gauge.

Local token counting still runs and still matters — it drives the sparkline,
today's totals, burn rate, cost estimate, and the fallback path.

### If live sync is unavailable

The collector keeps a sanitized copy of the last successful account reading.
During a temporary error such as HTTP 429, the gauges show that reading with an
`*` and its sync age. Each cached gauge expires at its server-provided reset
boundary, so a percentage is never carried into a new cycle. After it expires,
the gauge falls back to local token counts against a limit you calibrate, and
the percentage is prefixed with `~` to say it is a guess. Browser/app activity
is included in live and last-synced account readings, but not in local fallback
statistics.

Only these fields are persisted: percentage, reset boundary, display label,
severity, plan name, and sync time. OAuth credentials and raw responses are not
stored. On POSIX systems, the state file is written owner-only (`0600`).

To calibrate, run `/usage` in Claude Code, then feed the percentages it shows
back in:

```bash
python3 collector.py --calibrate --session 21 --week 58 --fable 4
```

That divides your measured token totals by those percentages, writes the derived
limits into `config.json`, and the `~` disappears. WSL mode also provides a
**Calibrate limits…** item in the widget's right-click menu.

Two tips:

- Calibrate **after a burst of usage**, not when you are near zero. `/usage`
  reports whole percentages, so deriving a limit from "3%" carries far more
  rounding error than from "60%".
- Set `week_anchor` first. Calibrating the weekly gauges against a rolling
  7-day window measures a different span than the one `/usage` reports, so the
  derived limit would be wrong.

### Matching the weekly reset

`/usage` shows a wall-clock reset ("Resets Fri 7:00 PM"). Put any past instance
of that moment in `week_anchor` and the collector counts successive 7-day
periods from it:

```json
"week_anchor": "2026-07-24T19:00:00+09:00"
```

Left as `null`, the weekly gauges fall back to a rolling 7 days and show
`rolling` instead of a countdown.

### Why 429s aren't used for this

Transcripts do record rate-limit errors, and using the block total at each one as
an observed ceiling looks like an easy win. It does not work: short-term
per-minute limits and 5-hour usage limits both surface as a bare 429 with the
same generic message, and the short-term ones fire early in a block while the
running total is still small. On this machine that approach put the session limit
at 15.6M against 45.7M actually in use. The count of recent 429s is reported in
the JSON as `limit_hits_7d`, but it is informational only.

### How much to trust the numbers

**The three percentages and reset times are exact** while live sync is on — they
are the server's own figures, the same ones `/usage` prints. There is no
estimation, no calibration, and no blind spot: usage from claude.ai, the desktop
app, or another machine is already counted, because the server counts it.

This is why live sync replaced calibration. Calibration fitted a straight line
through a single point, treating input + output + cache writes + cache reads as
one flat sum. Anthropic weights those differently, so the fit drifted as the
model mix changed — measured on this machine, the local estimate read **40%**
for a session the server put at **31%**. Live sync removes that class of error
entirely.

**Still local, still approximate:** the sparkline, today's totals, burn rate,
session count, and the API-equivalent cost all come from transcript parsing, and
only see this machine.

**One thing to know:** `percent` arrives as a whole number, so a bar can sit on
32% for a while before ticking to 33%. That is the server's resolution, not a
rounding bug on our side.

### The cost figure is API-equivalent, not your bill

It prices your token counts at published API rates (including cache read/write
multipliers, fast-mode pricing, and Sonnet 5's introductory rate). On a Pro or
Max subscription you are not billed this — it is a measure of how much work you
pushed through. Unknown models fall back to Opus-tier rates and set
`cost_exact: false` in the JSON.

## Using it

It is a desktop pet. By default only the bot is on screen; the dashboard folds
out of it.

| Action | Result |
| --- | --- |
| **Click the bot** | Pop the usage dashboard open, click again to fold it away |
| **Hover over it** | It hops, with `>` `<` eyes (2s cooldown so brushing past won't make it pogo) |
| **Drag it** | Carry it anywhere — it wakes up, squints happily, wiggles, and tilts into the motion |
| Right-click | Dashboard, wander on/off, jump, refresh, settings/calibration, data folder, quit |

Left alone it wanders: strolls a short way (mirroring itself to face the
direction it walks), stops to idle and breathe, and hops now and then — the
ground shadow shrinks as it leaves the floor. It stands still while the
dashboard is open, and stays put entirely if you untick **Let it wander**.

The sprite is the Claude pixel mascot: flat rectangles rendered with
`RenderOptions.EdgeMode="Aliased"` so the edges stay hard instead of being
anti-aliased into mush. Eyes are plain bars normally and chevrons when it is
pleased — hovered, dragged, or mid-hop.

Its colour and face track the **highest** of the three gauges: Claude orange
under 50%, deepening through amber to red above 90%, eyes shut with a `z` when
you have been idle. Sleep holds a completely still pose until activity resumes.
Position, wander setting, and whether the dashboard was open are all remembered
between runs, including positions on secondary monitors.

## Configuration — `config.json`

| Key | Default | Meaning |
| --- | --- | --- |
| `output_path` | `"auto"` | Where to write the snapshot. Uses `%USERPROFILE%` on native Windows, the Windows profile from WSL, or `~/.claude-widget` elsewhere. |
| `live_sync` | `true` | Read exact percentages from your account. Set `false` to stay fully local. |
| `live_interval_seconds` | `60` | How often to poll the usage endpoint. |
| `interval_seconds` | `5` | Collector pass interval. |
| `block_hours` | `5` | Session window length; matches Claude Code's block. |
| `limits.session` / `.week` / `.fable` | `"auto"` | Token limits. `auto` = your busiest stretch so far. Set by `--calibrate`. |
| `week_anchor` | `null` | ISO timestamp of a known weekly reset. `null` = rolling 7 days with no countdown. Set it to get a real "resets in" figure. |
| `fable_prefixes` | `["claude-fable-", "claude-mythos-"]` | Which models count toward the fable gauge. |
| `auto_floor` | see file | Lower bounds for estimated limits, so a quiet week doesn't make the bars jumpy. |
| `budget_metric` | `"total"` | `total` (input + output + cache write + cache read), `billable` (excludes cache reads), or `output`. |
| `retention_days` | `32` | How much transcript history to keep in the hourly rollup. |
| `idle_after_minutes` | `20` | Silence before the bot goes to sleep. |

Restart after editing: close and reopen the native Windows app, or run
`systemctl --user restart claude-usage-collector` in WSL mode.

## macOS and Linux

The headless collector works anywhere Python 3 and Claude Code use the standard
`~/.claude` directory:

```bash
python3 collector.py --loop
```

It writes `~/.claude-widget/usage.json`. The animated desktop pet is currently
Windows-only because its interface uses WPF; a macOS/Linux interface would need
a separate cross-platform UI.

## Collector CLI

```bash
python3 collector.py --once        # single pass, write the snapshot
python3 collector.py --print       # single pass, also print the snapshot
python3 collector.py --loop        # daemon mode (what run-collector.sh uses)
python3 collector.py --rebuild     # throw away state.json and rescan from disk
python3 collector.py --calibrate   # show window totals; add --session/--week/--fable to save limits
```

`state.json` holds per-file read cursors plus an hourly rollup, so passes after
the first only read newly appended bytes — a cold scan of ~1,700 transcripts
takes about two seconds, and each incremental pass about 0.15s.

## Implementation notes

- **Deduplication.** Claude Code writes the same `requestId` more than once per
  turn (streaming iterations land as repeated lines). Counting raw lines roughly
  doubles every figure, so entries are deduped by `requestId`, with a 48-hour id
  window persisted across runs.
- **Model IDs are normalized.** Context variants (`claude-opus-5[1m]`) and dated
  snapshots (`claude-haiku-4-5-20251001`) are folded onto their base id —
  otherwise the dated ones miss the pricing table and silently fall back to
  Opus-tier rates.
- **`<synthetic>` entries are dropped.** Claude Code emits them as placeholders;
  they are not real API calls.
- **Cache tiers are tracked separately.** `ephemeral_5m_input_tokens` and
  `ephemeral_1h_input_tokens` are priced at 1.25x and 2x base input respectively;
  cache reads at 0.1x. Older transcripts that only carry the aggregate are
  treated as the 5-minute tier.
- **Fast mode is priced separately.** `usage.speed == "fast"` on Opus 5 / 4.8
  bills at $10/$50 per MTok rather than $5/$25.
- **The session window is anchored to your first message**, not to the clock.
  It runs exactly 5 hours from that message's timestamp and stays the current
  window until it expires, whether or not you keep working; the next message
  after expiry opens a fresh one. An earlier version grouped hour-floored
  buckets and split on 5-hour gaps, which drifted badly — it expired a window at
  12:00 and reported an empty session while `/usage` still showed 24% used with
  2h11m left. Because the chain is order-dependent, entries from all transcripts
  are sorted by timestamp before being folded in, not processed file by file.
- **Weekly windows are hour-aligned**, so the hourly rollup measures them
  exactly once `week_anchor` is set.
- Only the Python standard library and stock WPF are used. Nothing to install.

### Two PowerShell traps worth remembering

Both of these produced wrong output on screen while looking perfectly correct in
the source, so they are commented at the call sites:

- **The comma binds tighter than arithmetic.** `@($period - 0.22, $x)` parses as
  `$period - (0.22, $x)` and throws *"[System.Object[]] does not contain a method
  named 'op_Subtraction'"*. Because that aborted the refresh partway through, the
  gauges silently kept stale text from an earlier pass. Parenthesise every
  arithmetic expression inside an array literal.
- **`[int]` rounds, it does not truncate.** `[int]1.75` is `2`, so
  `'{0}h {1:00}m' -f [int]$t.TotalHours, $t.Minutes` rendered 1h45m as
  **"2h 45m"** — and 4h51m as an impossible **"5h 51m"** on a 5-hour window. Use
  the `TimeSpan` components (`$t.Hours`, `$t.Minutes`) instead.

Both timer ticks are wrapped in `try/catch` so a future error degrades one frame
instead of freezing the pet or the card.

### Keeping the pet smooth

`AllowsTransparency="True"` makes this a layered window, which WPF renders in
software — every repaint costs real CPU, so the frame budget is tight. Three
things matter:

- **Let the OS do the dragging.** An earlier build polled
  `[System.Windows.Forms.Cursor]::Position` from the 33 ms timer and assigned
  `Left`/`Top` itself, which capped dragging at 30 Hz and left the bot visibly
  trailing the cursor. `DragMove()` hands the drag to the Windows move loop,
  which tracks at full input rate — and `DispatcherTimer`s keep firing inside
  that modal loop, so the wiggle still animates. Click-versus-drag is then
  decided by how far the window actually travelled.
- **Cache and freeze brushes.** A refresh touches a few dozen; rebuilding
  `SolidColorBrush` objects each time was pure garbage. Body fills are only
  reassigned when the colour actually changes.
- **Quantise idle motion.** The breathing bob wrote a fresh sub-pixel `Y` every
  frame, repainting the window 30x a second for a change nobody can see.
  Rounding it to whole pixels and skipping unchanged values took idle CPU from
  ~4.7% of a core to ~2.3%.

Measured cost per frame: **0.17 ms** while dragging, 0.33 ms idle, against a
33 ms budget; a data refresh is 3.6 ms every 2 s.

## Files

| File | Runs on | Purpose |
| --- | --- | --- |
| `collector.py` | All | Scans transcripts, writes `usage.json` |
| `native_app.py` | Windows | Hosts the standalone collector + widget app |
| `config.json` | All | Collector settings and calibrated limits |
| `state.json` | All | Read cursors + hourly rollup (generated) |
| `run-windows.ps1` | Windows | Runs source directly without WSL |
| `Start-Windows.vbs` | Windows | Hidden launcher for native source mode |
| `run-collector.sh` | WSL | Loop wrapper with a lock file |
| `status.sh` | WSL | Health check for both halves, with fix commands |
| `install.sh` | WSL | Copies the Windows half, optional autostart |
| `widget.ps1` | Windows | The WPF card |
| `Start-Widget.vbs` | Windows | Wakes WSL, launches the widget with no console |
