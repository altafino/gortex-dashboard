---
name: dashboard
description: Use when the user wants the Gortex dashboard — daemon health, graph size per repo, token savings, usage charts, or adding/removing tracked repositories.
---

# Gortex dashboard

Renders a self-contained HTML dashboard for the **local** Gortex daemon: version, uptime,
live MCP sessions, graph size per repository, tool-call token savings (total, per day, per
tool / language / client / repo), index-growth history, and buttons to add or remove
repositories.

The script ships next to this file:

- beside this `SKILL.md`: `scripts/gortex-dashboard.py`
- installed as a Claude Code plugin: `${CLAUDE_PLUGIN_ROOT}/skills/dashboard/scripts/gortex-dashboard.py`

Requires `gortex` on `PATH` and a running daemon (`gortex daemon status` must answer).
Python 3.9+ stdlib only — no pip installs.

## When to Use

- "open the gortex dashboard", "show my gortex stats / usage", "how many tokens has gortex saved"
- Daemon health, index freshness, which repos are stale
- Adding or removing repositories from the Gortex index
- Comparing graph size before and after a rebuild or upgrade

Do not use it to *answer* code questions — that is the `gortex` skill's job.

## Run it

```bash
# static page (read-only)
python3 scripts/gortex-dashboard.py --open

# live page with Track / remove buttons — http://127.0.0.1:8899/
python3 scripts/gortex-dashboard.py --serve
```

Writes `~/.local/share/gortex-dashboard/index.html`; `--serve` binds `127.0.0.1` only and then
opens the page itself (socket first, browser second — no connection-refused race). Pass
`--no-open` when you want to hand the URL to the user instead.

Flags: `--open` · `--serve` · `--port N` (default 8899) · `--no-open` · `--no-snapshot`
· `--print` (dump the collected JSON) · `--out PATH`.

## Managing repositories

`--serve` unlocks two write paths; both shell out to the gortex CLI and never touch the
database directly:

| UI | CLI | Notes |
|---|---|---|
| `+ track` on a discovered repo, or the path box | `gortex track <abs path>` | adds to `~/.config/gortex/config.yaml`, daemon indexes in the background |
| `remove` on a tracked repo | `gortex untrack <abs path> --confirm` | drops the index **and** the config entry; files on disk untouched |

The "add" list is every git repo under `~/go/src`, `~/src`, `~/Projects`, `~/code`, `~/dev`
(3 levels deep) that the config does not list yet. Both handlers re-read the config to
verify the change before reporting success, then the page reloads.

## Where the numbers come from

| Data | Source |
|---|---|
| version, uptime, sessions, memory, per-repo files/nodes/edges/disk, live MCP sessions, db+wal size | `gortex daemon status` (box tables parsed in `box_tables()`) |
| branch, head, last indexed, stale | `gortex repos --json` |
| tokens saved/returned, calls, per tool/language/client/repo/day | `~/.gortex/sidecar.sqlite` → `savings_events`, `savings_totals` |
| memories, memory kinds, promoted (learned) tools | same sidecar → `memories`, `promoted_tools` |
| tracked repo list, add/remove | `~/.config/gortex/config.yaml` (`path:` lines) + scan of git repos on disk |
| index growth trend | own SQLite `~/.local/share/gortex-dashboard/dashboard.sqlite` (`snapshots`, `repo_snapshots`) |

## Pitfalls

- The sidecar is a live WAL database owned by the daemon: open it `mode=ro` with the `-wal`/`-shm`
  files readable, never write to it, and never copy it.
- Two sidecars exist (`~/.gortex/sidecar.sqlite` active, `~/.local/share/gortex/sidecar.sqlite`
  legacy). The script picks whichever holds more `savings_events`; do not sum them.
- `gortex daemon status` prints differently to a TTY vs a pipe, and its table borders must be
  skipped, not treated as row terminators — that is why `box_tables()` ignores `┌├└` lines.
- `gortex untrack` is two-stage: the first call drops the index, a second removes the config
  entry. `do_untrack()` re-runs it when the path is still in the config.
- `gortex track` returns before indexing finishes; the repo appears in `status` with
  `files=0` until the build lane catches up. Track verifies via the config, not node counts.
- A repo tracked in the config but not indexed renders as a `config only` row
  (`(not indexed — tracked in config, no index held)`); never parse that cell as an int.
- Serving the page needs the `/echarts.min.js` route as well as `/` — a missing route shows up
  only as `echarts is not defined` in the browser console.
- Snapshots with zero nodes (daemon warming up or down) are dropped, otherwise the growth chart
  gets a fake zero point.
- ECharts is cached as `echarts.min.js` next to the HTML on first run (~1 MB) so the page works
  offline; the CDN is the fallback.
- `open_page()` tries `xdg-open`, then `open` (macOS), then `webbrowser`. `webbrowser.open()` can
  return False under an agent session while the desktop browser is fine, so never use its return
  value alone to decide whether the user saw the page.
- `--json` is not a flag on `gortex status`; use `gortex repos --json` for machine-readable data.

## Extending

Add the query in `collect_savings()` / `collect_status()`, put the series into the `D` payload,
then add a `mk('id', option)` block plus a `<div class="chart" id="id">`. Keep the dark palette
(`--vio`, `--cyn`, `--grn`) and the `fmt` / `bytes` / `dur` formatters so numbers stay consistent.
