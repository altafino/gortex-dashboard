# gortex-dashboard (Claude Code plugin)

A dashboard for the [Gortex](https://github.com/zzet/gortex) code-intelligence daemon that is
running on your machine. It renders version, uptime, live MCP sessions, graph size per
repository, tool-call token savings (total, per day, per tool, per language, per client), index
growth over time, and — in `--serve` mode — one-click buttons to add or remove tracked
repositories.

Everything is local: it reads `gortex daemon status`, `gortex repos --json` and the daemon's
sidecar SQLite, then writes one self-contained HTML page with ECharts charts.

## Install

```
/plugin install gortex-dashboard --marketplace altafino/gortex-dashboard
```

## Use

Ask Claude for the gortex dashboard, or run `/gortex-dashboard:dashboard`. To manage
repositories from the page, start it in serve mode:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/dashboard/scripts/gortex-dashboard.py" --serve
```

Requires `gortex` on `PATH` with a running daemon and Python 3.9+. Nothing to configure.
Full documentation, flags and data sources: [repository README](https://github.com/altafino/gortex-dashboard).
