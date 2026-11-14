# ritcbot4: RITCx Liability Trading bot

A fully automated bot for the RITCx Liability Trading Case (RIT LT3, Dynamic Order Arrival). It
connects straight to the RIT server through the **DMA REST API**, so the RIT Client does not need
to be running.

For each tender it estimates the expected P&L after fees, liquidity and risk, then accepts or
declines explicitly within the 30 s window. It unwinds accepted tenders through **one compliance
gate** and guarantees a flat position at the end of the heat.

- Brief: [`reference/RITCx-Liability_Trading_Case.pdf`](reference/RITCx-Liability_Trading_Case.pdf)
- Rotman support script (cleaned up): [`reference/`](reference/)
- Every threshold: [`config.toml`](config.toml)

---

## 1. Setup on macOS (VS Code terminal)

```bash
git clone https://github.com/jhealyoconnor/ritcbot4.git
```
```bash
cd ritcbot4
```
```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -r requirements-dev.txt
```
```bash
cp .env.example .env
```

Open `.env` in VS Code and fill it in. Git ignores `.env`, so it is never committed.

```
RIT_HOST=flserver.rotman.utoronto.ca
RIT_PORT=            # the port for your event (or pass --port)
RIT_USER=your-trader-id
RIT_PASS=your-password
```

Python 3.9 or newer works, including the one that ships with macOS. Each new terminal needs
`source .venv/bin/activate` first.

## 2. Run

The port is resolved in this order: `--port` on the command line, then `RIT_PORT` in `.env`, then a
prompt at startup. The port you are connected to is shown at the top of the screen.

| What | Command |
|---|---|
| Login and case status only, plus raw samples of a book and any tender | `python -m bot --check --port 16540` |
| Live data, logs what it **would** do, sends nothing | `python -m bot --dry-run --port 16540` |
| **Live trading** | `python -m bot --port 16540` |
| Live, stop after one heat | `python -m bot --port 16540 --once` |
| Change any setting for one run | `python -m bot --port 16540 --set tender.min_ev_total=100` |

**Stopping:**
- **Ctrl+C once** (or create a file named `STOP` in the repo folder): declines new tenders,
  flattens every position through the compliance gate, then exits.
- **Ctrl+C twice** (or a file named `STOP_NOW`): cancels all orders and exits immediately.
  This can leave a position open, which is penalised, so flatten in the RIT Client if you do it.

You can start the bot before the heat begins; it waits for `ACTIVE`. Between heats it keeps
running and starts again on the next heat unless you pass `--once`.

## 3. Competition-day checklist

1. `git pull`, `source .venv/bin/activate`, check that `.env` has the right user and password.
2. **Set the port** for the event: `--port NNNNN` or `RIT_PORT` in `.env`.
3. `python -m bot --check --port NNNNN`. Confirm:
   - login OK, and the case name, tick and `ticks_per_period` look right;
   - limits show **100,000 / 250,000**, and `max_trade_size` shows **25,000 / 10,000**;
   - `trading_fee` is **0.02**. Note `limit_order_rebate` and `execution_delay_ms`;
   - the book keys (`bids/asks` or `bid/ask`) both parse;
   - if a tender is pending: `parsed: our action ...` matches the caption, and note `action field = trader/institution`.
4. Run a short **`--dry-run`** in a practice heat. Check that the tender decisions and EVs on screen
   look sensible, then read `logs/<time>-dry/decisions.csv`.
5. **Live:** `python -m bot --port NNNNN`. Keep the RIT Client open to watch, but do not trade by hand.
   Any position the bot didn't create is treated as inventory and unwound.
6. After each heat, read the printed summary. **END POSITIONS must be 0.** Note
   `fee on tender observed`, `Tender action field meaning` and the `Competitor checks`.

## 4. How it trades

**Tender decision.** The bot values the whole book before and after accepting:

```
EV(accept) = tender cash − tender fee + V(position after) − V(position now)
```

`V(x)` is what unwinding `x` shares should net, made up of:
- shares the visible book takes now at prices as good as working them later;
- the rest worked as the book refills, at the VWAP of the top N shares minus slippage and a risk
  buffer `λ·σ·√(unwind_ticks/3)`;
- anything that can't be worked before the end-of-heat deadline, dumped through the book;
- minus 2c per unwind share.

This one formula handles existing inventory, netting (an opposite tender is worth more because it
replaces your worst unwind shares) and late tenders. The brief's examples are unit tests: the
$9.90 tender is accepted and the $9.97 tender is declined.

- **Clearly positive:** accepted immediately. You get more unwind time, first access to the book,
  and the stock is frozen for less time.
- **Marginal:** re-checked every tick and declined explicitly 3 ticks before expiry.
- **Declined if:** it would breach net or gross limits (including open orders), would put more than
  100k in one stock, or the caption doesn't match the tender fields.

**Unwind.**
1. **Sweep:** the moment a tender is accepted, take every visible share priced at or better than
   break-even (tender ± both fees), using marketable limits capped at that price.
2. **Schedule:** work the rest over a short horizon (CRZY 15 ticks, TAME 25), shorter when
   volatility is high or competitors are draining the book. Use marketable limits one tick through
   the touch, at most 60% of visible depth, and never chase far below the recent mid.
3. **Passive:** one limit order inside the spread while there is time.
4. **Endgame:** from T−30, price further through the book. From T−10, send market orders capped at
   the exact remaining position every loop until flat.

**Compliance gate** (`bot/gate.py`). This is the only path to `POST /orders`. An order is allowed
only if all of these hold:
- the case is ACTIVE;
- the stock holds accepted-tender inventory;
- the order reduces the position;
- order size + every same-side order that might still fill ≤ |position|;
- size ≤ 25k (CRZY) or 10k (TAME);
- no tender is pending in that stock;
- the worst case over all partial fills stays within the limits.

Fills are counted pessimistically using request sequence numbers, so an unconfirmed order always
counts in full. Order sends and accepts are never retried blindly: after a dropped connection the
bot checks the server first.

**Front-running freeze.** While a tender is pending in a stock, the bot sends no new orders there
and cancels all its resting orders there before deciding.

Orders also go out only (a) after tenders have been re-read in the current tick and (b) early
enough in the tick to reach the server before the next tick boundary, when new tenders appear.
This shrinks the "tender appeared but not yet seen" gap to almost nothing. The end-of-heat flatten
is never blocked, and the rule relaxes if a slow connection keeps it shut for 2 ticks.

## 5. The settings to tune first

| Setting | Default | What it does |
|---|---|---|
| `tender.min_ev_per_share` / `tender.min_ev_total` | 0.005 / $150 | The acceptance bar. Lower means more tenders accepted. |
| `tender.risk_lambda` | 0.3 | Inventory-risk buffer in the valuation. Ranked best of 0.1 / 0.3 / 0.6 in the mock sweep. |
| `tender.refill_prior` (CRZY/TAME) | 3000 / 1500 | Shares per tick that come back near the touch. Learned live; set it from the `refill` column of your practice logs. |
| `unwind.horizon_ticks` (CRZY/TAME) | 15 / 25 | How fast inventory is worked after the sweep. |
| `fees.on_tender` | true | Set to false if the summary says `fee on tender observed: no`. |

## 6. Logs, replay, tuning

Each run writes to `logs/<time>-<mode>/`:
- `events.jsonl`: everything;
- `ticks.jsonl`: books, positions, P&L, latency and requests per second for every loop;
- `orders.csv`: every order with its tender id, plus every blocked order with the reason;
- `decisions.csv`: every tender decision with its numbers;
- `summary.txt` / `summary.json`.

```bash
python -m sim.replay logs/20261120-101500-live
```
```bash
python -m sim.sweep --replay "logs/*-live" --grid tender.risk_lambda=0.1,0.3,0.6 --grid unwind.passive=true,false
```
```bash
python -m sim.sweep --heats 20 --grid unwind.horizon_ticks.CRZY=8,15,25
```
```bash
python -m sim.backtest --heats 10 --verbose
```

Replay plays back the books and tenders a heat actually saw. The sweep ranks settings by mean P&L
per heat, charging violations a penalty and showing the spread.

## 7. Offline testing

```bash
python -m pytest -q
```
```bash
python -m pytest -q -m slow
```

The first command runs all tests offline in about 1.5 min. The second runs the real-time 5-minute
heat with 150 ms latency.

The mock RIT server lets you try the bot end to end on your own Mac:

```bash
python -m sim.mock_server --port 9999 --latency 0.15
```
```bash
RIT_USER=BOT RIT_PASS=pass python -m bot --host localhost --port 9999 --once
```

## 8. Layout

```
bot/   api.py (DMA client)  gate.py (compliance)  valuation.py (tender EV)  unwind.py (orders)
       engine.py (loop)  tracker.py (orders)  ledger.py (tender lots)  history.py (vol/refill/competitors)
       marketdata.py (parsing)  display.py  logs.py  cli.py
sim/   market.py (simulated LT3 market + compliance referee)  mock_server.py (HTTP)  client.py
       backtest.py  replay.py  sweep.py
tests/ gate, valuation (brief examples), parsing/tracker, unwind, API failure modes,
       full heats (scenarios), HTTP end-to-end, replay/sweep
reference/  case brief + cleaned support script
```
