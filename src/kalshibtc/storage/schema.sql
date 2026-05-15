PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS ticks_1s (
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    bid REAL,
    ask REAL,
    source TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    strike REAL NOT NULL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (market_ticker, ts)
);

CREATE INDEX IF NOT EXISTS idx_ticks_1s_ts ON ticks_1s(ts);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    contracts REAL NOT NULL,
    notional REAL NOT NULL,
    raw_json TEXT NOT NULL
);
