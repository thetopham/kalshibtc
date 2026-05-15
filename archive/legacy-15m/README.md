# Archived legacy 15m bot

This directory contains code and support files that are no longer part of the active package surface.

Archived here:

- `src/kalshi_btc_15m_bot/` — old scanner/model-blend/dashboard/live-order package.
- `tests/` — tests that exercised the archived package.
- `configs/` — old paper/demo/live config templates for the `kbtc15` CLI.
- `deploy/` — old dashboard, live-demo, and 60s paper service templates.
- `docs/LEGACY.md` and `docs/source-notes.md` — historical notes.
- `.env.example` — legacy credential/config example.

The active 1s paper bot now lives under:

```text
src/kalshibtc/
```

Active entrypoint:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

Archive policy:

- Keep this directory as reference only.
- Do not import archived modules from `src/kalshibtc/`.
- Do not add new strategy, execution, dashboard, live-order, or ML work here.
- If something from the archive is needed again, move only the small required piece into `src/kalshibtc/` behind a tested 1s-specific seam.
