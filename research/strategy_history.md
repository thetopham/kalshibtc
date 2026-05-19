# Strategy Research History

Generated: 2026-05-19T00:19:30Z

Safety boundary: replay/research summaries only; no live orders.

## breakout_momentum

Runs scanned: 6

### Best by realized PnL

1. `all-strategies-nexttick-breakout_momentum-1778995478`
   - Strategy: `breakout_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$135.14
   - Fills: 3,883
   - Notional: +$97,075.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.007344
   - Max drawdown: +$5,147.82
   - Raw run: `runs/breakout_momentum/all-strategies-nexttick-breakout_momentum-1778995478`
2. `live`
   - Strategy: `breakout_momentum`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/breakout_momentum`
3. `improved-20260516T055637Z`
   - Strategy: `breakout_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$11.99
   - Fills: 47
   - Notional: +$1,175.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.952026
   - Max drawdown: +$250.00
   - Raw run: `runs/breakout_momentum/improved-20260516T055637Z`
4. `bankroll200-allstats-breakout_momentum-1778996866`
   - Strategy: `breakout_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$12.52
   - Fills: 39
   - Notional: +$195.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.721733
   - Max drawdown: +$45.00
   - Raw run: `runs/breakout_momentum/bankroll200-allstats-breakout_momentum-1778996866`
5. `all-strategies-smallcap-breakout_momentum-1778995976`
   - Strategy: `breakout_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$49.81
   - Fills: 756
   - Notional: +$3,780.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.954512
   - Max drawdown: +$237.62
   - Raw run: `runs/breakout_momentum/all-strategies-smallcap-breakout_momentum-1778995976`
6. `all-strategies-20260517T033359Z-breakout_momentum`
   - Strategy: `breakout_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$228.76
   - Fills: 3,871
   - Notional: +$96,775.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.987992
   - Max drawdown: +$5,494.31
   - Raw run: `runs/breakout_momentum/all-strategies-20260517T033359Z-breakout_momentum`

## cheap_accumulate_repair_v0

Runs scanned: 9

### Best by realized PnL

1. `B_no_seed_480-20260518T055800Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$56.07
   - Fills: 1,314
   - Notional: +$2,983.93
   - Completed-pair PnL: +$172.05
   - Unpaired-leftover PnL: -$115.98
   - Profit factor: 1.0341
   - Max drawdown: +$461.74
   - Params:
     - cheap_price = 0.15
     - max_total_cost = 50
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 480
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/B_no_seed_480-20260518T055800Z`
2. `cheap-sweep-repair8m-20260518T053851Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$56.07
   - Fills: 1,314
   - Notional: +$2,983.93
   - Completed-pair PnL: +$172.05
   - Unpaired-leftover PnL: -$115.98
   - Profit factor: 1.0341
   - Max drawdown: +$461.74
   - Params:
     - cheap_price = 0.15
     - direct_pair_spend = 20
     - max_total_cost = 50
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 480
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-sweep-repair8m-20260518T053851Z`
3. `cheap-sweep-repair8m_pair5-20260518T053925Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$56.07
   - Fills: 1,314
   - Notional: +$2,983.93
   - Completed-pair PnL: +$172.05
   - Unpaired-leftover PnL: -$115.98
   - Profit factor: 1.0341
   - Max drawdown: +$461.74
   - Params:
     - cheap_price = 0.15
     - direct_pair_spend = 5
     - max_total_cost = 50
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 480
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-sweep-repair8m_pair5-20260518T053925Z`
4. `cheap-sweep-cheap12_repair10-20260518T054107Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$19.98
   - Fills: 188
   - Notional: +$554.43
   - Completed-pair PnL: +$45.69
   - Unpaired-leftover PnL: -$65.67
   - Profit factor: 0.91754
   - Max drawdown: +$73.88
   - Params:
     - cheap_price = 0.12
     - direct_pair_cost = 0.92
     - direct_pair_spend = 5
     - max_total_cost = 35
     - min_seconds_between_orders = 10
     - normal_spend = 1
     - repair_start_seconds = 600
     - very_cheap_price = 0.06
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-sweep-cheap12_repair10-20260518T054107Z`
5. `cheap-sweep-ultra-20260518T053959Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$22.65
   - Fills: 352
   - Notional: +$672.28
   - Completed-pair PnL: +$44.88
   - Unpaired-leftover PnL: -$67.53
   - Profit factor: 0.941419
   - Max drawdown: +$183.78
   - Params:
     - cheap_price = 0.1
     - direct_pair_cost = 0.9
     - direct_pair_spend = 5
     - max_total_cost = 25
     - min_seconds_between_orders = 10
     - normal_spend = 1
     - repair_start_seconds = 480
     - very_cheap_price = 0.05
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-sweep-ultra-20260518T053959Z`
6. `cheap-sweep-cheap12_repair8-20260518T054033Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$72.52
   - Fills: 546
   - Notional: +$1,270.32
   - Completed-pair PnL: +$97.02
   - Unpaired-leftover PnL: -$169.54
   - Profit factor: 0.894792
   - Max drawdown: +$232.27
   - Params:
     - cheap_price = 0.12
     - direct_pair_cost = 0.92
     - direct_pair_spend = 5
     - max_total_cost = 35
     - min_seconds_between_orders = 10
     - normal_spend = 1
     - repair_start_seconds = 480
     - very_cheap_price = 0.06
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-sweep-cheap12_repair8-20260518T054033Z`
7. `A_no_seed_240-20260518T055725Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$445.23
   - Fills: 3,681
   - Notional: +$6,307.80
   - Completed-pair PnL: +$786.77
   - Unpaired-leftover PnL: -$1,232.00
   - Profit factor: 0.91029
   - Max drawdown: +$1,385.34
   - Params:
     - cheap_price = 0.15
     - max_total_cost = 50
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 240
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/A_no_seed_240-20260518T055725Z`
8. `cheap-accumulate-repair-v0-tight-20260518T053741Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$445.23
   - Fills: 3,679
   - Notional: +$6,301.09
   - Completed-pair PnL: +$786.77
   - Unpaired-leftover PnL: -$1,232.00
   - Profit factor: 0.91029
   - Max drawdown: +$1,385.34
   - Params:
     - cheap_price = 0.15
     - direct_pair_spend = 20
     - max_total_cost = 50
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 240
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-accumulate-repair-v0-tight-20260518T053741Z`
9. `cheap-accumulate-repair-v0-20260518T053651Z`
   - Strategy: `cheap_accumulate_repair_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$6,373.89
   - Fills: 3,904
   - Notional: +$39,510.00
   - Completed-pair PnL: +$1,556.51
   - Unpaired-leftover PnL: -$7,930.40
   - Profit factor: 0.803518
   - Max drawdown: +$10,074.87
   - Raw run: `runs/cheap_accumulate_repair_v0/cheap-accumulate-repair-v0-20260518T053651Z`

## complement_ladder_v0

Runs scanned: 5

### Best by realized PnL

1. `complement-ladder-tuned-20260518T050406Z`
   - Strategy: `complement_ladder_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$5.64
   - Fills: 13,407
   - Notional: +$850.39
   - Completed-pair PnL: +$35.16
   - Unpaired-leftover PnL: -$40.80
   - Profit factor: 0.965089
   - Max drawdown: +$33.15
   - Raw run: `runs/complement_ladder_v0/complement-ladder-tuned-20260518T050406Z`
2. `complement-ladder-tuned-backfill-20260518T051043Z`
   - Strategy: `complement_ladder_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$14.03
   - Fills: 13,407
   - Notional: +$850.39
   - Completed-pair PnL: +$77.64
   - Unpaired-leftover PnL: -$91.67
   - Profit factor: 0.962531
   - Max drawdown: +$33.15
   - Raw run: `runs/complement_ladder_v0/complement-ladder-tuned-backfill-20260518T051043Z`
3. `complement-ladder-smoke-20260518T043245Z`
   - Strategy: `complement_ladder_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$138.99
   - Fills: 40,626
   - Notional: +$7,388.15
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.920096
   - Max drawdown: +$290.86
   - Params:
     - cheap_add_contracts = 5
     - cheap_threshold = 0.25
     - direct_pair_contracts = 10
     - max_unpaired_contracts = 50
     - target_pair_cost = 0.95
   - Raw run: `runs/complement_ladder_v0/complement-ladder-smoke-20260518T043245Z`
4. `complement-ladder-v0-20260518T045426Z`
   - Strategy: `complement_ladder_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$215.98
   - Fills: 33,437
   - Notional: +$7,700.92
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.879847
   - Max drawdown: +$343.55
   - Params:
     - cheap_add_contracts = 5
     - cheap_threshold = 0.25
     - direct_pair_contracts = 10
     - max_unpaired_contracts = 50
     - target_pair_cost = 0.95
   - Raw run: `runs/complement_ladder_v0/complement-ladder-v0-20260518T045426Z`
5. `complement-ladder-aggressive-backfill-20260518T051006Z`
   - Strategy: `complement_ladder_v0`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$502.75
   - Fills: 33,674
   - Notional: +$7,754.53
   - Completed-pair PnL: +$632.74
   - Unpaired-leftover PnL: -$1,135.50
   - Profit factor: 0.869103
   - Max drawdown: +$618.68
   - Params:
     - cheap_add_contracts = 5
     - cheap_threshold = 0.25
     - completion_only_seconds = 60
     - direct_pair_contracts = 10
     - extreme_cheap_contracts = 10
     - extreme_cheap_threshold = 0.1
     - max_unpaired_contracts = 50
     - min_seconds_to_open_unpaired = 60
     - target_pair_cost = 0.95
   - Raw run: `runs/complement_ladder_v0/complement-ladder-aggressive-backfill-20260518T051006Z`

## contrarian_spread_reversion

Runs scanned: 2

### Best by realized PnL

1. `live`
   - Strategy: `contrarian_spread_reversion`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 58
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/contrarian_spread_reversion`
2. `contrarian-smoke-20260516T065647Z`
   - Strategy: `contrarian_spread_reversion`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$1,710.01
   - Fills: 522
   - Notional: +$13,050.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.840929
   - Max drawdown: +$5,739.55
   - Raw run: `runs/contrarian_spread_reversion/contrarian-smoke-20260516T065647Z`

## dynamic_complement_hedge

Runs scanned: 2

### Best by realized PnL

1. `paper_trade-smoke-paper`
   - Strategy: `dynamic_complement_hedge`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 72
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/dynamic_complement_hedge/paper_trade-smoke-paper`
2. `temporal_scan-smoke`
   - Strategy: `dynamic_complement_hedge`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/dynamic_complement_hedge/temporal_scan-smoke`

## inventory_aware_passive_mm

Runs scanned: 2

### Best by realized PnL

1. `inventory-aware-passive-mm-smoke-2`
   - Strategy: `inventory_aware_passive_mm`
   - Exchange: polymarket
   - Datafeed: polymarket-btc-1s
   - Feed DB: `feed/polymarket-btc-1s.sqlite3`
   - Realized PnL: +$8.50
   - Fills: 1
   - Notional: +$17.25
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$8.50
   - Profit factor: inf
   - Max drawdown: +$0.00
   - Params:
     - min_order_contracts = 1
     - min_visible_depth = 0
   - Raw run: `runs/inventory_aware_passive_mm/inventory-aware-passive-mm-smoke-2`
2. `inventory-aware-passive-mm-smoke`
   - Strategy: `inventory_aware_passive_mm`
   - Exchange: polymarket
   - Datafeed: polymarket-btc-1s
   - Feed DB: `feed/polymarket-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Params:
     - min_order_contracts = 1
     - min_visible_depth = 0
   - Raw run: `runs/inventory_aware_passive_mm/inventory-aware-passive-mm-smoke`

## inventory_vol_regime

Runs scanned: 1

### Best by realized PnL

1. `live`
   - Strategy: `inventory_vol_regime`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 24
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/inventory_vol_regime`

## late_window_only

Runs scanned: 9

### Best by realized PnL

1. `all-strategies-20260517T033359Z-late_window_only`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$44,089.73
   - Fills: 239
   - Notional: +$5,975.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 16.203355
   - Max drawdown: +$1,550.00
   - Raw run: `runs/late_window_only/all-strategies-20260517T033359Z-late_window_only`
2. `bankroll200-allstats-late_window_only-1778996866`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$35.62
   - Fills: 36
   - Notional: +$180.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.44522
   - Max drawdown: +$45.00
   - Raw run: `runs/late_window_only/bankroll200-allstats-late_window_only-1778996866`
3. `improved-20260516T055637Z`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$2.78
   - Fills: 1
   - Notional: +$25.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: inf
   - Max drawdown: +$0.00
   - Raw run: `runs/late_window_only/improved-20260516T055637Z`
4. `live`
   - Strategy: `late_window_only`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/late_window_only`
5. `all-strategies-smallcap-late_window_only-1778995976`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$42.77
   - Fills: 89
   - Notional: +$445.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.768828
   - Max drawdown: +$85.00
   - Raw run: `runs/late_window_only/all-strategies-smallcap-late_window_only-1778995976`
6. `audit-late_window_only-same-tick-1778989978`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$3,250.60
   - Fills: 239
   - Notional: +$5,975.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.226047
   - Max drawdown: +$3,253.38
   - Raw run: `runs/late_window_only/audit-late_window_only-same-tick-1778989978`
7. `audit-late_window_only-next-tick-1778989994`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$3,254.25
   - Fills: 237
   - Notional: +$5,925.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.215844
   - Max drawdown: +$3,256.96
   - Raw run: `runs/late_window_only/audit-late_window_only-next-tick-1778989994`
8. `audit2-late_window_only-next-tick-1778990051`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$3,254.25
   - Fills: 237
   - Notional: +$5,925.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.215844
   - Max drawdown: +$3,256.96
   - Raw run: `runs/late_window_only/audit2-late_window_only-next-tick-1778990051`
9. `all-strategies-nexttick-late_window_only-1778995478`
   - Strategy: `late_window_only`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$3,254.25
   - Fills: 239
   - Notional: +$5,975.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.215844
   - Max drawdown: +$3,256.96
   - Raw run: `runs/late_window_only/all-strategies-nexttick-late_window_only-1778995478`

## mean_reversion_to_strike

Runs scanned: 6

### Best by realized PnL

1. `live`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/mean_reversion_to_strike`
2. `all-strategies-20260517T033359Z-mean_reversion_to_strike`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/mean_reversion_to_strike/all-strategies-20260517T033359Z-mean_reversion_to_strike`
3. `all-strategies-nexttick-mean_reversion_to_strike-1778995478`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/mean_reversion_to_strike/all-strategies-nexttick-mean_reversion_to_strike-1778995478`
4. `all-strategies-smallcap-mean_reversion_to_strike-1778995976`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/mean_reversion_to_strike/all-strategies-smallcap-mean_reversion_to_strike-1778995976`
5. `bankroll200-allstats-mean_reversion_to_strike-1778996866`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/mean_reversion_to_strike/bankroll200-allstats-mean_reversion_to_strike-1778996866`
6. `improved-20260516T055637Z`
   - Strategy: `mean_reversion_to_strike`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/mean_reversion_to_strike/improved-20260516T055637Z`

## no_trade_baseline

Runs scanned: 6

### Best by realized PnL

1. `live`
   - Strategy: `no_trade_baseline`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/no_trade_baseline`
2. `all-strategies-20260517T033359Z-no_trade_baseline`
   - Strategy: `no_trade_baseline`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/no_trade_baseline/all-strategies-20260517T033359Z-no_trade_baseline`
3. `all-strategies-nexttick-no_trade_baseline-1778995478`
   - Strategy: `no_trade_baseline`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/no_trade_baseline/all-strategies-nexttick-no_trade_baseline-1778995478`
4. `all-strategies-smallcap-no_trade_baseline-1778995976`
   - Strategy: `no_trade_baseline`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/no_trade_baseline/all-strategies-smallcap-no_trade_baseline-1778995976`
5. `bankroll200-allstats-no_trade_baseline-1778996866`
   - Strategy: `no_trade_baseline`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/no_trade_baseline/bankroll200-allstats-no_trade_baseline-1778996866`
6. `improved-20260516T055637Z`
   - Strategy: `no_trade_baseline`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/no_trade_baseline/improved-20260516T055637Z`

## pair_arb

Runs scanned: 1

### Best by realized PnL

1. `live`
   - Strategy: `pair_arb`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 4
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/pair_arb`

## pair_arb_grid

Runs scanned: 1

### Best by realized PnL

1. `live`
   - Strategy: `pair_arb_grid`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/pair_arb_grid`

## pair_arb_passive

Runs scanned: 1

### Best by realized PnL

1. `live`
   - Strategy: `pair_arb_passive`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 10
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/pair_arb_passive`

## seed_cheap_accumulate_repair_v1

Runs scanned: 74

### Best by realized PnL

1. `seed200_90_30_r360_net0.45-20260518T063124Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$672.52
   - Fills: 1,921
   - Notional: +$33,417.13
   - Completed-pair PnL: +$2,124.73
   - Unpaired-leftover PnL: -$1,452.22
   - Profit factor: 1.044489
   - Max drawdown: +$588.53
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_90_30_r360_net0.45-20260518T063124Z`
2. `kalshi-20260516-17-seed-repair-v1-90-30`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$612.49
   - Fills: 837
   - Notional: +$19,367.56
   - Completed-pair PnL: +$1,406.63
   - Unpaired-leftover PnL: -$794.14
   - Profit factor: 1.069196
   - Max drawdown: +$473.38
   - Params:
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/kalshi-20260516-17-seed-repair-v1-90-30`
3. `seed200_90_30_r360_net0.35-20260518T063048Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$610.06
   - Fills: 1,715
   - Notional: +$33,152.00
   - Completed-pair PnL: +$2,145.00
   - Unpaired-leftover PnL: -$1,534.94
   - Profit factor: 1.041006
   - Max drawdown: +$521.16
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_90_30_r360_net0.35-20260518T063048Z`
4. `tune_90_30_r360_net0.45-20260518T065339Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$595.72
   - Fills: 1,400
   - Notional: +$32,838.04
   - Completed-pair PnL: +$2,127.09
   - Unpaired-leftover PnL: -$1,531.37
   - Profit factor: 1.040625
   - Max drawdown: +$499.83
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r360_net0.45-20260518T065339Z`
5. `tune_100_33p333333_r360_net0.45-20260518T065902Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$574.65
   - Fills: 1,425
   - Notional: +$35,177.25
   - Completed-pair PnL: +$2,554.29
   - Unpaired-leftover PnL: -$1,979.64
   - Profit factor: 1.036003
   - Max drawdown: +$613.90
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 33.333333
     - seed_primary_spend = 100
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_100_33p333333_r360_net0.45-20260518T065902Z`
6. `tune_90_30_r360_net0.40-20260518T065302Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$561.66
   - Fills: 1,336
   - Notional: +$32,753.48
   - Completed-pair PnL: +$2,128.29
   - Unpaired-leftover PnL: -$1,566.63
   - Profit factor: 1.038481
   - Max drawdown: +$485.96
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.4
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r360_net0.40-20260518T065302Z`
7. `tune_90_30_r360_net0.35-20260518T065226Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$548.12
   - Fills: 1,272
   - Notional: +$32,646.29
   - Completed-pair PnL: +$2,133.58
   - Unpaired-leftover PnL: -$1,585.46
   - Profit factor: 1.037769
   - Max drawdown: +$483.96
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r360_net0.35-20260518T065226Z`
8. `tune_100_33p333333_r360_net0.40-20260518T065827Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$530.23
   - Fills: 1,361
   - Notional: +$35,104.25
   - Completed-pair PnL: +$2,550.28
   - Unpaired-leftover PnL: -$2,020.05
   - Profit factor: 1.03337
   - Max drawdown: +$591.36
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.4
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 33.333333
     - seed_primary_spend = 100
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_100_33p333333_r360_net0.40-20260518T065827Z`
9. `tune_80_26p666667_r360_net0.45-20260518T064835Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$516.66
   - Fills: 1,365
   - Notional: +$29,977.74
   - Completed-pair PnL: +$1,863.70
   - Unpaired-leftover PnL: -$1,347.04
   - Profit factor: 1.038861
   - Max drawdown: +$424.57
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 26.666667
     - seed_primary_spend = 80
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_80_26p666667_r360_net0.45-20260518T064835Z`
10. `tune_80_26p666667_r360_net0.40-20260518T064759Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$514.29
   - Fills: 1,292
   - Notional: +$29,809.80
   - Completed-pair PnL: +$1,855.98
   - Unpaired-leftover PnL: -$1,341.70
   - Profit factor: 1.039044
   - Max drawdown: +$418.42
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.4
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 26.666667
     - seed_primary_spend = 80
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_80_26p666667_r360_net0.40-20260518T064759Z`
11. `seed200_90_30_r360_net0.25-20260518T063013Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$506.26
   - Fills: 1,558
   - Notional: +$32,765.33
   - Completed-pair PnL: +$2,110.33
   - Unpaired-leftover PnL: -$1,604.08
   - Profit factor: 1.034502
   - Max drawdown: +$480.25
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.25
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_90_30_r360_net0.25-20260518T063013Z`
12. `tune_100_33p333333_r360_net0.35-20260518T065751Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$503.02
   - Fills: 1,304
   - Notional: +$35,043.25
   - Completed-pair PnL: +$2,553.15
   - Unpaired-leftover PnL: -$2,050.12
   - Profit factor: 1.031782
   - Max drawdown: +$572.95
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 33.333333
     - seed_primary_spend = 100
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_100_33p333333_r360_net0.35-20260518T065751Z`
13. `tune_80_26p666667_r360_net0.35-20260518T064723Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$463.75
   - Fills: 1,235
   - Notional: +$29,689.47
   - Completed-pair PnL: +$1,829.62
   - Unpaired-leftover PnL: -$1,365.87
   - Profit factor: 1.035427
   - Max drawdown: +$419.94
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 26.666667
     - seed_primary_spend = 80
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_80_26p666667_r360_net0.35-20260518T064723Z`
14. `tune_70_23p333333_r360_net0.45-20260518T064313Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$458.96
   - Fills: 1,321
   - Notional: +$26,617.89
   - Completed-pair PnL: +$1,668.88
   - Unpaired-leftover PnL: -$1,209.93
   - Profit factor: 1.038888
   - Max drawdown: +$391.98
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 23.333333
     - seed_primary_spend = 70
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_70_23p333333_r360_net0.45-20260518T064313Z`
15. `tune_70_23p333333_r360_net0.35-20260518T064202Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$398.52
   - Fills: 1,204
   - Notional: +$26,281.11
   - Completed-pair PnL: +$1,602.55
   - Unpaired-leftover PnL: -$1,204.04
   - Profit factor: 1.034421
   - Max drawdown: +$369.80
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 23.333333
     - seed_primary_spend = 70
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_70_23p333333_r360_net0.35-20260518T064202Z`
16. `seed200_60_20_r360_net0.45-20260518T062753Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$390.50
   - Fills: 1,722
   - Notional: +$24,216.00
   - Completed-pair PnL: +$1,546.33
   - Unpaired-leftover PnL: -$1,155.83
   - Profit factor: 1.036115
   - Max drawdown: +$413.47
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 20
     - seed_primary_spend = 60
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_60_20_r360_net0.45-20260518T062753Z`
17. `tune_70_23p333333_r360_net0.40-20260518T064238Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$388.29
   - Fills: 1,253
   - Notional: +$26,425.34
   - Completed-pair PnL: +$1,634.87
   - Unpaired-leftover PnL: -$1,246.58
   - Profit factor: 1.03323
   - Max drawdown: +$385.32
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.4
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 360
     - seed_hedge_spend = 23.333333
     - seed_primary_spend = 70
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_70_23p333333_r360_net0.40-20260518T064238Z`
18. `tune_100_33p333333_r390_net0.35-20260518T065938Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$342.41
   - Fills: 1,212
   - Notional: +$34,926.82
   - Completed-pair PnL: +$2,367.97
   - Unpaired-leftover PnL: -$2,025.56
   - Profit factor: 1.021704
   - Max drawdown: +$642.09
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 390
     - seed_hedge_spend = 33.333333
     - seed_primary_spend = 100
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_100_33p333333_r390_net0.35-20260518T065938Z`
19. `tune_90_30_r390_net0.45-20260518T065528Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$338.86
   - Fills: 1,303
   - Notional: +$32,688.98
   - Completed-pair PnL: +$1,937.93
   - Unpaired-leftover PnL: -$1,599.07
   - Profit factor: 1.023174
   - Max drawdown: +$552.77
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 390
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r390_net0.45-20260518T065528Z`
20. `tune_100_33p333333_r330_net0.45-20260518T065716Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$337.59
   - Fills: 1,526
   - Notional: +$34,721.01
   - Completed-pair PnL: +$2,562.43
   - Unpaired-leftover PnL: -$2,224.84
   - Profit factor: 1.021131
   - Max drawdown: +$799.00
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 330
     - seed_hedge_spend = 33.333333
     - seed_primary_spend = 100
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_100_33p333333_r330_net0.45-20260518T065716Z`
21. `seed200_60_20_r360_net0.25-20260518T062642Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$331.32
   - Fills: 1,360
   - Notional: +$22,958.46
   - Completed-pair PnL: +$1,328.33
   - Unpaired-leftover PnL: -$997.01
   - Profit factor: 1.032855
   - Max drawdown: +$294.82
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.25
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 20
     - seed_primary_spend = 60
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_60_20_r360_net0.25-20260518T062642Z`
22. `tune_90_30_r390_net0.40-20260518T065451Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$308.10
   - Fills: 1,247
   - Notional: +$32,619.73
   - Completed-pair PnL: +$1,935.85
   - Unpaired-leftover PnL: -$1,627.75
   - Profit factor: 1.021163
   - Max drawdown: +$558.81
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.4
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 390
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r390_net0.40-20260518T065451Z`
23. `seed200_60_20_r360_net0.35-20260518T062717Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$292.66
   - Fills: 1,527
   - Notional: +$23,644.42
   - Completed-pair PnL: +$1,442.69
   - Unpaired-leftover PnL: -$1,150.03
   - Profit factor: 1.027984
   - Max drawdown: +$325.78
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - repair_start_seconds = 360
     - seed_hedge_spend = 20
     - seed_primary_spend = 60
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/seed200_60_20_r360_net0.35-20260518T062717Z`
24. `tune_90_30_r390_net0.35-20260518T065415Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$285.74
   - Fills: 1,189
   - Notional: +$32,528.76
   - Completed-pair PnL: +$1,939.58
   - Unpaired-leftover PnL: -$1,653.84
   - Profit factor: 1.01973
   - Max drawdown: +$547.18
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.35
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 390
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r390_net0.35-20260518T065415Z`
25. `tune_90_30_r330_net0.45-20260518T065150Z`
   - Strategy: `seed_cheap_accumulate_repair_v1`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$277.51
   - Fills: 1,490
   - Notional: +$32,308.44
   - Completed-pair PnL: +$2,144.89
   - Unpaired-leftover PnL: -$1,867.39
   - Profit factor: 1.018884
   - Max drawdown: +$682.02
   - Params:
     - cheap_price = 0.15
     - max_net_ratio = 0.45
     - max_total_cost = 200
     - min_order_contracts = 5
     - min_seconds_between_orders = 5
     - normal_spend = 1
     - one_fill_per_price_level = True
     - repair_start_seconds = 330
     - seed_hedge_spend = 30
     - seed_primary_spend = 90
     - very_cheap_price = 0.08
     - very_cheap_spend = 2
   - Raw run: `runs/seed_cheap_accumulate_repair_v1/tune_90_30_r330_net0.45-20260518T065150Z`

## simple_directional

Runs scanned: 9

### Best by realized PnL

1. `all-strategies-20260517T033359Z-simple_directional`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$297,561.37
   - Fills: 38,251
   - Notional: +$956,275.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 2.452404
   - Max drawdown: +$23,218.03
   - Raw run: `runs/simple_directional/all-strategies-20260517T033359Z-simple_directional`
2. `audit-simple_directional-next-tick-1778989961`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$16,254.45
   - Fills: 38,400
   - Notional: +$960,000.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.076196
   - Max drawdown: +$19,080.00
   - Raw run: `runs/simple_directional/audit-simple_directional-next-tick-1778989961`
3. `audit2-simple_directional-next-tick-1778990033`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$16,254.45
   - Fills: 38,453
   - Notional: +$961,325.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.076196
   - Max drawdown: +$19,080.00
   - Raw run: `runs/simple_directional/audit2-simple_directional-next-tick-1778990033`
4. `all-strategies-nexttick-simple_directional-1778995478`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$16,219.24
   - Fills: 40,892
   - Notional: +$1,022,300.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.075968
   - Max drawdown: +$19,105.00
   - Raw run: `runs/simple_directional/all-strategies-nexttick-simple_directional-1778995478`
5. `audit-simple_directional-same-tick-1778989944`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$15,788.89
   - Fills: 38,738
   - Notional: +$968,450.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.072751
   - Max drawdown: +$19,131.72
   - Raw run: `runs/simple_directional/audit-simple_directional-same-tick-1778989944`
6. `improved-20260516T055637Z`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$1,516.17
   - Fills: 100
   - Notional: +$2,500.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 7.064662
   - Max drawdown: +$250.00
   - Raw run: `runs/simple_directional/improved-20260516T055637Z`
7. `all-strategies-smallcap-simple_directional-1778995976`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$854.08
   - Fills: 1,040
   - Notional: +$5,200.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.532135
   - Max drawdown: +$260.73
   - Raw run: `runs/simple_directional/all-strategies-smallcap-simple_directional-1778995976`
8. `bankroll200-allstats-simple_directional-1778996866`
   - Strategy: `simple_directional`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$70.81
   - Fills: 39
   - Notional: +$195.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 2.573587
   - Max drawdown: +$45.00
   - Raw run: `runs/simple_directional/bankroll200-allstats-simple_directional-1778996866`
9. `live`
   - Strategy: `simple_directional`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/simple_directional`

## simple_inventory_mm

Runs scanned: 43

### Best by realized PnL

1. `limit5-net200-gross200-20260517T160457Z`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$14.94
   - Fills: 85
   - Notional: +$195.74
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.231145
   - Max drawdown: +$19.71
   - Params:
     - cheap_add_contracts = 5
     - cheap_limit_price = 0.4
     - max_gross_contracts = 200
     - max_net_contracts = 200
     - min_seconds_between_orders = 0
     - repair_add_contracts = 5
     - repair_window_seconds = 300
     - rich_add_contracts = 5
     - seed_add_contracts = 5
     - very_cheap_add_contracts = 5
   - Raw run: `runs/simple_inventory_mm/limit5-net200-gross200-20260517T160457Z`
2. `limit5-net200-gross200-contract-daily-20260517T161006Z`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$14.94
   - Fills: 85
   - Notional: +$195.74
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.231145
   - Max drawdown: +$19.71
   - Params:
     - cheap_add_contracts = 5
     - cheap_limit_price = 0.4
     - max_gross_contracts = 200
     - max_net_contracts = 200
     - min_seconds_between_orders = 0
     - repair_add_contracts = 5
     - repair_window_seconds = 300
     - rich_add_contracts = 5
     - seed_add_contracts = 5
     - very_cheap_add_contracts = 5
   - Raw run: `runs/simple_inventory_mm/limit5-net200-gross200-contract-daily-20260517T161006Z`
3. `bankroll200-allstats-simple_inventory_mm-1778996866`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$1.58
   - Fills: 3,002
   - Notional: +$195.98
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.01529
   - Max drawdown: +$18.70
   - Params:
     - cheap_add_contracts = 2
     - cheap_limit_price = 0.4
     - max_gross_contracts = 30
     - max_net_contracts = 10
     - min_seconds_between_orders = 0
     - repair_add_contracts = 2
     - repair_window_seconds = 300
     - rich_add_contracts = 1
     - seed_add_contracts = 2
     - very_cheap_add_contracts = 3
   - Raw run: `runs/simple_inventory_mm/bankroll200-allstats-simple_inventory_mm-1778996866`
4. `limit5-net200-gross400-20260517T160524Z`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$0.79
   - Fills: 219
   - Notional: +$195.11
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.013642
   - Max drawdown: +$31.76
   - Params:
     - cheap_add_contracts = 5
     - cheap_limit_price = 0.4
     - max_gross_contracts = 400
     - max_net_contracts = 200
     - min_seconds_between_orders = 0
     - repair_add_contracts = 5
     - repair_window_seconds = 300
     - rich_add_contracts = 5
     - seed_add_contracts = 5
     - very_cheap_add_contracts = 5
   - Raw run: `runs/simple_inventory_mm/limit5-net200-gross400-20260517T160524Z`
5. `all-strategies-smallcap-simple_inventory_mm-1778995976`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$7.77
   - Fills: 1,004
   - Notional: +$749.10
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.975688
   - Max drawdown: +$33.97
   - Params:
     - cheap_add_contracts = 2
     - cheap_limit_price = 0.4
     - max_gross_contracts = 30
     - max_net_contracts = 10
     - min_seconds_between_orders = 0
     - repair_add_contracts = 2
     - repair_window_seconds = 300
     - rich_add_contracts = 1
     - seed_add_contracts = 2
     - very_cheap_add_contracts = 3
   - Raw run: `runs/simple_inventory_mm/all-strategies-smallcap-simple_inventory_mm-1778995976`
6. `limit5-net200-gross200-recycle-bankroll2000-20260517T162153Z`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$9.81
   - Fills: 32,219
   - Notional: +$14,020.05
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.99757
   - Max drawdown: +$302.48
   - Params:
     - cheap_add_contracts = 5
     - cheap_limit_price = 0.4
     - max_gross_contracts = 200
     - max_net_contracts = 200
     - min_seconds_between_orders = 0
     - repair_add_contracts = 5
     - repair_window_seconds = 300
     - rich_add_contracts = 5
     - seed_add_contracts = 5
     - very_cheap_add_contracts = 5
   - Raw run: `runs/simple_inventory_mm/limit5-net200-gross200-recycle-bankroll2000-20260517T162153Z`
7. `matrix2-mm-net30-repair300-cheap40-1778994679`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$52.75
   - Fills: 1,950
   - Notional: +$6,037.58
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.980046
   - Max drawdown: +$203.50
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 30
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net30-repair300-cheap40-1778994679`
8. `resting-state-smoke`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$57.21
   - Fills: 397
   - Notional: +$1,439.92
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.882983
   - Max drawdown: +$80.83
   - Raw run: `runs/simple_inventory_mm/resting-state-smoke`
9. `matrix2-mm-net20-repair300-cheap40-1778994607`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$68.33
   - Fills: 1,740
   - Notional: +$5,384.87
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.971249
   - Max drawdown: +$133.77
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 20
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net20-repair300-cheap40-1778994607`
10. `matrix2-mm-net30-repair300-cheap35-1778994661`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$71.27
   - Fills: 1,864
   - Notional: +$5,811.80
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.972029
   - Max drawdown: +$185.38
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 30
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net30-repair300-cheap35-1778994661`
11. `limit5-net200-gross200-recycle-20260517T162104Z`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$80.54
   - Fills: 251
   - Notional: +$620.48
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.7599
   - Max drawdown: +$163.96
   - Params:
     - cheap_add_contracts = 5
     - cheap_limit_price = 0.4
     - max_gross_contracts = 200
     - max_net_contracts = 200
     - min_seconds_between_orders = 0
     - repair_add_contracts = 5
     - repair_window_seconds = 300
     - rich_add_contracts = 5
     - seed_add_contracts = 5
     - very_cheap_add_contracts = 5
   - Raw run: `runs/simple_inventory_mm/limit5-net200-gross200-recycle-20260517T162104Z`
12. `matrix2-mm-net20-repair300-cheap35-1778994589`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$83.62
   - Fills: 1,654
   - Notional: +$5,275.94
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.964494
   - Max drawdown: +$131.08
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 20
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net20-repair300-cheap35-1778994589`
13. `matrix2-mm-net10-repair300-cheap35-1778994518`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$83.81
   - Fills: 1,090
   - Notional: +$3,606.98
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.947791
   - Max drawdown: +$110.30
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 10
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net10-repair300-cheap35-1778994518`
14. `matrix2-mm-net30-repair180-cheap40-1778994643`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$95.57
   - Fills: 1,943
   - Notional: +$5,873.18
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.96415
   - Max drawdown: +$245.73
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 30
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net30-repair180-cheap40-1778994643`
15. `matrix2-mm-net10-repair300-cheap40-1778994536`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$103.85
   - Fills: 1,320
   - Notional: +$4,075.24
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.942222
   - Max drawdown: +$116.57
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 10
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net10-repair300-cheap40-1778994536`
16. `audit-simple_inventory_mm-portfolio-1778993167`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$105.37
   - Fills: 4,725
   - Notional: +$7,310.69
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.9694
   - Max drawdown: +$380.95
   - Raw run: `runs/simple_inventory_mm/audit-simple_inventory_mm-portfolio-1778993167`
17. `audit-simple_inventory_mm-next-tick-state-1778991373`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$105.37
   - Fills: 4,582
   - Notional: +$7,202.07
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.9694
   - Max drawdown: +$380.95
   - Raw run: `runs/simple_inventory_mm/audit-simple_inventory_mm-next-tick-state-1778991373`
18. `matrix2-mm-net10-repair180-cheap35-1778994483`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$105.84
   - Fills: 1,180
   - Notional: +$3,778.58
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.939363
   - Max drawdown: +$135.23
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 10
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net10-repair180-cheap35-1778994483`
19. `matrix2-mm-net10-repair180-cheap40-1778994500`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$117.98
   - Fills: 1,398
   - Notional: +$4,184.61
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.937517
   - Max drawdown: +$134.75
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 10
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net10-repair180-cheap40-1778994500`
20. `matrix2-mm-net20-repair180-cheap40-1778994571`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$120.66
   - Fills: 1,764
   - Notional: +$5,312.37
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.95028
   - Max drawdown: +$164.66
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 20
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net20-repair180-cheap40-1778994571`
21. `matrix-mm-net20-repair300-cheap35-1778993875`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$122.25
   - Fills: 4,381
   - Notional: +$6,455.09
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.958559
   - Max drawdown: +$253.45
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 20
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix-mm-net20-repair300-cheap35-1778993875`
22. `matrix-mm-net20-repair300-cheap40-1778993892`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$124.32
   - Fills: 4,752
   - Notional: +$6,759.36
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.958791
   - Max drawdown: +$277.32
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 20
     - repair_window_seconds = 300
   - Raw run: `runs/simple_inventory_mm/matrix-mm-net20-repair300-cheap40-1778993892`
23. `matrix-mm-net20-repair180-cheap35-1778993805`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$126.86
   - Fills: 4,364
   - Notional: +$6,468.92
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.957479
   - Max drawdown: +$255.47
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 20
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix-mm-net20-repair180-cheap35-1778993805`
24. `matrix-mm-net20-repair180-cheap40-1778993822`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$132.00
   - Fills: 4,732
   - Notional: +$6,759.15
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.956509
   - Max drawdown: +$285.18
   - Params:
     - cheap_limit_price = 0.4
     - max_net_contracts = 20
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix-mm-net20-repair180-cheap40-1778993822`
25. `matrix2-mm-net20-repair180-cheap35-1778994553`
   - Strategy: `simple_inventory_mm`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$143.50
   - Fills: 1,708
   - Notional: +$5,258.38
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.940757
   - Max drawdown: +$190.06
   - Params:
     - cheap_limit_price = 0.35
     - max_net_contracts = 20
     - repair_window_seconds = 180
   - Raw run: `runs/simple_inventory_mm/matrix2-mm-net20-repair180-cheap35-1778994553`

## spread_aware_momentum

Runs scanned: 7

### Best by realized PnL

1. `all-strategies-nexttick-spread_aware_momentum-1778995478`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$97.11
   - Fills: 903
   - Notional: +$22,575.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.008992
   - Max drawdown: +$4,659.72
   - Raw run: `runs/spread_aware_momentum/all-strategies-nexttick-spread_aware_momentum-1778995478`
2. `bankroll200-allstats-spread_aware_momentum-1778996866`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$92.30
   - Fills: 39
   - Notional: +$195.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 2.419928
   - Max drawdown: +$65.00
   - Raw run: `runs/spread_aware_momentum/bankroll200-allstats-spread_aware_momentum-1778996866`
3. `bankroll200-smoke-spread`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$92.30
   - Fills: 39
   - Notional: +$195.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 2.419928
   - Max drawdown: +$65.00
   - Raw run: `runs/spread_aware_momentum/bankroll200-smoke-spread`
4. `all-strategies-smallcap-spread_aware_momentum-1778995976`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$73.52
   - Fills: 437
   - Notional: +$2,185.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.075404
   - Max drawdown: +$284.78
   - Raw run: `runs/spread_aware_momentum/all-strategies-smallcap-spread_aware_momentum-1778995976`
5. `live`
   - Strategy: `spread_aware_momentum`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/spread_aware_momentum`
6. `improved-20260516T055637Z`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$329.55
   - Fills: 15
   - Notional: +$375.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.058442
   - Max drawdown: +$350.00
   - Raw run: `runs/spread_aware_momentum/improved-20260516T055637Z`
7. `all-strategies-20260517T033359Z-spread_aware_momentum`
   - Strategy: `spread_aware_momentum`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$710.47
   - Fills: 902
   - Notional: +$22,550.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.940547
   - Max drawdown: +$5,536.96
   - Raw run: `runs/spread_aware_momentum/all-strategies-20260517T033359Z-spread_aware_momentum`

## strategy_probability_mm_v0

Runs scanned: 1

### Best by realized PnL

1. `probability-mm-v0-smoke`
   - Strategy: `strategy_probability_mm_v0`
   - Exchange: polymarket
   - Datafeed: polymarket-btc-1s
   - Feed DB: `feed/polymarket-btc-1s.sqlite3`
   - Realized PnL: -$51.19
   - Fills: 40
   - Notional: +$394.91
   - Completed-pair PnL: -$1.13
   - Unpaired-leftover PnL: -$50.07
   - Profit factor: 0.756226
   - Max drawdown: +$80.15
   - Params:
     - base_notional = 10
     - edge_threshold = 0.02
     - max_net_ratio = 0.35
   - Raw run: `runs/strategy_probability_mm_v0/probability-mm-v0-smoke`

## volatility_hedge

Runs scanned: 2

### Best by realized PnL

1. `live`
   - Strategy: `volatility_hedge`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 2,150
   - Notional: +$107,423.04
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/volatility_hedge`
2. `live`
   - Strategy: `volatility_hedge`
   - Exchange: unknown
   - Datafeed: unknown
   - Feed DB: ``
   - Realized PnL: +$0.00
   - Fills: 0
   - Notional: +$0.00
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0
   - Max drawdown: +$0.00
   - Raw run: `runs/live/volatility_hedge`

## volatility_inventory

Runs scanned: 7

### Best by realized PnL

1. `all-strategies-nexttick-volatility_inventory-1778995478`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: +$2,339.39
   - Fills: 15,181
   - Notional: +$368,648.83
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 1.007083
   - Max drawdown: +$265,704.67
   - Raw run: `runs/volatility_inventory/all-strategies-nexttick-volatility_inventory-1778995478`
2. `bankroll200-allstats-volatility_inventory-1778996866`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$165.97
   - Fills: 35
   - Notional: +$173.91
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.017385
   - Max drawdown: +$165.97
   - Raw run: `runs/volatility_inventory/bankroll200-allstats-volatility_inventory-1778996866`
3. `all-strategies-smallcap-volatility_inventory-1778995976`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$854.46
   - Fills: 890
   - Notional: +$4,397.50
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.764194
   - Max drawdown: +$2,699.13
   - Raw run: `runs/volatility_inventory/all-strategies-smallcap-volatility_inventory-1778995976`
4. `improved-imbalance-guard-20260517T023331Z`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$49,167.78
   - Fills: 13,711
   - Notional: +$359,655.86
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.858455
   - Max drawdown: +$293,873.13
   - Raw run: `runs/volatility_inventory/improved-imbalance-guard-20260517T023331Z`
5. `improved-market-reset-20260517T023156Z`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$49,167.78
   - Fills: 13,711
   - Notional: +$359,655.86
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.858455
   - Max drawdown: +$293,873.13
   - Raw run: `runs/volatility_inventory/improved-market-reset-20260517T023156Z`
6. `baseline-20260517T023024Z`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$49,623.38
   - Fills: 13,687
   - Notional: +$358,480.86
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.857051
   - Max drawdown: +$294,276.69
   - Raw run: `runs/volatility_inventory/baseline-20260517T023024Z`
7. `all-strategies-20260517T033359Z-volatility_inventory`
   - Strategy: `volatility_inventory`
   - Exchange: kalshi
   - Datafeed: kalshi-btc-1s
   - Feed DB: `feed/kalshi-btc-1s.sqlite3`
   - Realized PnL: -$53,284.97
   - Fills: 14,121
   - Notional: +$363,773.05
   - Completed-pair PnL: +$0.00
   - Unpaired-leftover PnL: +$0.00
   - Profit factor: 0.848399
   - Max drawdown: +$293,873.13
   - Raw run: `runs/volatility_inventory/all-strategies-20260517T033359Z-volatility_inventory`
