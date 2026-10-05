# polylag

![tests](https://github.com/Saireddy21072007/polylag/actions/workflows/tests.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![asyncio](https://img.shields.io/badge/async-httpx%20%7C%20websockets-informational)

> Side project. An event-driven trading system built to **measure** whether a news-to-price lag exists — paper mode by default, three layers of risk control, 68 tests. Not investment advice.

A news-lag trading system for Polymarket. It watches news feeds, matches
headlines against hand-written rules, checks whether a market has *not yet*
repriced, and buys the lagging side under hard risk limits.

---



**This will probably lose money.** That is not modesty, it is the base rate.

The 30-90 second lag between a headline and a repriced market is real, but it is
mostly harvested by people with paid low-latency wire feeds, co-located
infrastructure, and automated execution measured in milliseconds. This system,
configured with public RSS, is typically **slower than the market it is trying
to beat**. By the time a free RSS feed publishes, the traders who move the
market have usually already moved it.

So why build it? Because the honest way to find out whether a lag exists in
*your* markets, at *your* latency, is to measure it — in `watch` mode, then
`paper` mode, with every rejection logged. The system is designed to produce
that evidence. If the evidence is negative, the correct action is to stop, and
the tooling (`run.py report`) is built to tell you so bluntly.

Other things that are true:

- **No claim of profitability is made anywhere in this codebase.** Edges of this
  kind decay as more participants automate the same idea.
- **I am not a licensed financial advisor and this is not investment advice.**
- **Check your jurisdiction and Polymarket's Terms of Service** before trading,
  including whether automated trading is permitted for your account. That is
  your responsibility, not the software's.
- **Known network issue:** on the machine this was developed on, TLS
  connections to `polymarket.com` are reset at handshake — consistent with
  ISP/network-level blocking. If `run.py doctor` reports the CLOB unreachable,
  test with a plain `curl https://clob.polymarket.com/ok` before assuming a bug.
- **What is and is not verified.** Be precise about this before you fund it:
  - *Verified offline:* the whole engine, end to end, via `run.bat selftest` —
    68 unit tests plus 6 scenarios driving the real decision path.
  - *Verified against the SDK:* every symbol, enum and signature the live
    order path uses (`tests/test_live_contract.py`), so an SDK upgrade cannot
    silently break live mode.
  - *NOT verified:* the Gamma and CLOB HTTP response shapes, because the
    network here could not reach them. `run.py doctor` is the command that
    checks this — run it first. If a field has moved,
    `clients/gamma.py::_to_market_ref` and `clients/clob.py::_levels` are the
    two places to fix.
  - *NOT verified by anyone, ever:* that this makes money.

---

## 1. Architecture

### Components

| Component | Module | Job |
|---|---|---|
| News monitor | `news/feeds.py` | Poll RSS/Atom with conditional GET, de-duplicate, emit `NewsEvent` |
| Trigger matcher | `news/matcher.py` | Match headline text to hand-written rules per market |
| Market scanner | `clients/gamma.py` | Resolve config slugs into tradable token ids |
| Book store | `clients/ws.py` | Live books over websocket + REST resync + rolling mid history |
| Fair value | `strategy/fair_value.py` | One line of arithmetic, no model, fully auditable |
| Entry/exit rules | `strategy/rules.py` | Named gates, each with a rejection reason |
| Risk manager | `risk/manager.py` | Sizing, throttles, daily halt, drawdown kill latch |
| Order manager | `execution/manager.py` | Intents, confirmed fills, hedge fallback |
| Brokers | `execution/paper.py`, `execution/live.py` | Identical interface, one simulates, one spends |
| Portfolio | `portfolio.py` | Positions, cash, bid-marked equity, realised P&L |
| Journal | `journal.py` | CSV decisions/fills/trades + durable risk state |
| Metrics | `metrics.py` | Expectancy, drawdown, fee drag, edge-decay verdict |
| Engine | `engine.py` | Wires it together, owns the run loops |

### Data flow

```
  RSS feeds (20-30s poll, conditional GET)
        │
        ▼
  NewsMonitor ── de-dupe ──► NewsEvent(seen_ms = decision clock starts)
        │
        ▼
  TriggerMatcher  ──► (market, trigger) pairs        Polymarket CLOB websocket
        │                                                       │
        │                                          book / price_change events
        │                                                       ▼
        │                                            BookStore (+ mid history,
        │                                             REST resync every 60s)
        │                                                       │
        └────────────────────┬──────────────────────────────────┘
                             ▼
                  rules.evaluate_entry
                    ├─ market live? not resolving soon?
                    ├─ book fresh, two-sided, not crossed?
                    ├─ do we have a PRE-NEWS mid?      ◄── the anchor
                    ├─ has it already repriced?        ◄── the actual lag test
                    ├─ spread / depth tradable?
                    ├─ edge ≥ min_edge after cost buffer?
                    └─ price inside sane bounds?
                             │ accepted
                             ▼
                  RiskManager.can_open ──► size_order
                    kill latch, daily halt, drawdown, feed health,
                    staleness, position count, throttles, cooldowns
                             │ approved + sized
                             ▼
                     OrderManager.open_position
                             │
                 ┌───────────┴───────────┐
            PaperBroker              LiveBroker
         (latency, partial          (signed FAK order,
          fills, slippage,           fills reconciled
          fees)                      against trade history)
                 └───────────┬───────────┘
                             ▼ CONFIRMED fill only
                        Portfolio ────► Journal (CSV + JSONL)
                             │
   ┌─────────────────────────┘
   ▼
 Position loop (every 1s, independent of news)
   ├─ mark at BID, recompute equity
   ├─ RiskManager.update_equity ──► daily halt / kill latch
   └─ rules.evaluate_exit ──► stop / take-profit / time stop / resolution guard
                             │
                             ▼
                   OrderManager.close_position
                     └─ if no bid: hedge with the opposite leg
```

Two clocks run independently. News drives entries; a 1-second loop drives exits
and risk. **Getting out never depends on the machinery that got you in.**

---

## 2. Risk management

Risk is not a feature here, it is the spine. Three layers:

### Layer 1 — per-trade gates (reject one order, keep running)

Position size is the **smallest** of:

- `max_trade_pct_of_equity` — 1% of current equity
- `max_notional_per_trade_usdc` — $10 hard cap
- `max_notional_per_market_usdc` minus what you already hold there
- `max_gross_exposure_pct` headroom — 10% of equity across everything
- available cash (with 2% held back for fees)
- `max_depth_participation` × visible depth — never take more than 25% of the book

The binding constraint is logged on every trade, so you can see which limit is
actually shaping your behaviour.

An order is refused outright when: the kill latch is set, the day is halted, the
websocket is down, the book is older than 1.5s, the headline is older than 90s,
you already hold either leg, you are at `max_open_positions`, you are inside a
cooldown, you are over the hourly/daily trade count, or the size lands below the
venue's minimum.

### Layer 2 — daily halt (stops new entries until the next UTC day)

Triggered at `daily_loss_limit_pct` (2%) of the day's opening equity. Not
permanent, not persisted as a latch. Open positions still get managed and
exited; only new entries stop.

### Layer 3 — the kill switch (permanent, requires a human)

Latches on any of:

- drawdown from **peak** equity ≥ `max_drawdown_pct` (6%)
- equity ≤ `min_equity_floor_usdc` (absolute floor)
- an order returning an **unknown** fill state — position may not match records
- manual: `python run.py kill "reason"`

What happens: everything is flattened, `state/KILL` is written to disk with the
reason and timestamp, and the process stops. **Restarting does not clear it.**
The engine refuses to start while it exists. Clearing requires
`python run.py resume` and typing `YES` after you have reconciled positions on
Polymarket yourself.

Peak equity, the day anchor and trade counts are persisted in `state/state.json`
and reloaded on start — **a restart never hands you a fresh drawdown
allowance.** There is a test for exactly this
(`test_peak_equity_persists_across_restarts`).

### A sizing trap worth knowing about

With a $200 bankroll and 1% sizing you can only ever size a $2 order, and the
venue minimum is around $5. Nothing errors — every signal is silently rejected
as too small and you conclude the strategy "never fires". `run.py doctor` checks
this and fails loudly. It is why the default bankroll is $600.

---

## 3. Core logic

### Detecting a lagged market

The lag test is not "did news happen" — it is **"did news happen and the market
has not moved yet"**. That needs a *before* price, so `BookStore` keeps 15
minutes of mid history per token.

1. A headline arrives. The decision clock starts at `seen_ms` (when *we* saw
   it), not the publication timestamp, which is often rounded or wrong.
2. Look up the mid from ~2s before we saw the headline. **No pre-news mid → no
   trade.** Without a "before" you cannot measure a lag, you can only guess.
3. Compare the current mid to that anchor. If the market has already moved more
   than `max_price_move_since_news` (3c) toward where we think it belongs,
   **the lag is gone** and we decline. Chasing is how this strategy turns into
   buying tops.

### Estimating the mispricing

The entire model:

```
fair_value = pre_news_mid + confidence × (target_price − pre_news_mid)
```

- `pre_news_mid` — the market's own price before the news. We anchor on the
  market because it knows more than we do about everything except this headline.
- `target_price` — where *you* wrote in `config.yaml` this market belongs if the
  headline is true.
- `confidence` — 0..1, how far to travel from anchor to target.

That is all. No embeddings, no LLM, no fitted parameters. It is not a
probability model and does not claim to be right. It is a way of **writing your
prior down before you see the price**, so you cannot rationalise a trade
afterwards. If the output looks silly, your config is wrong — which is the
point, because the error is visible and editable.

Edge is then `fair_value − ask − cost_buffer`, and must clear `min_edge`.

### Entry rules

Buy the favoured outcome token when every gate passes: market live and not
resolving within 30 minutes; book fresh (<1.5s), two-sided, uncrossed;
pre-news anchor exists; market has not already repriced; spread ≤ 4c; top-of-book
depth ≥ 50 shares; edge ≥ 6c after a 2c cost buffer; price between 0.08 and 0.90.

Orders are **marketable limit, FAK** — cross at most one tick past the touch,
fill what is there, kill the rest. Never GTC: a resting order is a stale opinion
that other people get to trade against.

### Exit rules

Checked every second, first match wins:

1. **Resolution guard** — never carry a position into settlement. Holding to
   resolution is a completely different bet from the one you entered.
2. **Stop loss** — bid moves 5c against entry.
3. **Take profit** — capture 60% of the expected move (`take_profit_edge_capture`).
4. **Time stop** — 15 minutes. If the repricing has not come, your thesis was
   wrong, and you are now just holding a random position.

Positions are marked at the **bid**, not the mid. Mid-marking flatters every
open position and is the most common way a paper P&L curve lies to you.

### Hedging

YES and NO always settle to exactly 1.00 combined. If you hold 100 YES bought at
0.62 and the YES bid disappears, buying 100 NO at 0.34 fixes your payoff at 100
against 96 spent — the position becomes risk-free and simply waits.

This is useful in exactly one situation: **you need out and your own side has no
liquidity.** It is not a way to fix a losing trade. If YES+NO costs more than
1.00 you have locked in a loss, merely capped. The code therefore only hedges
when the locked loss is no worse than the stop loss you already accepted.

---

## 4. Implementation

### Install — Windows

Double-click **`run.bat`** for a menu, or use it from a terminal:

```bash
run.bat setup
```

That creates `.venv`, installs dependencies, and copies `.env.example` to
`.env`. Every later command runs inside that venv automatically, so you never
have to remember to activate it.

```bash
run.bat selftest
```

Unit tests plus the full offline simulation. **This works with no network, no
API keys and no money** — it is the fastest way to confirm the whole system is
functioning before you point it at anything real.

Other platforms: `pip install -r requirements.txt`, then use `python run.py ...`
in place of `run.bat ...`. Every command below exists in both forms.

### Prove it works before it can cost you anything

```bash
run.bat simulate
```

Runs the real engine against scripted markets — only the websocket and the RSS
feeds are replaced. Six scenarios, each exercising a different path:

| Scenario | Story | Should do |
|---|---|---|
| `clean-lag` | Market sits still 3s after the headline, then reprices | Enter, exit at take-profit |
| `no-reprice` | The move never comes | Enter, exit on the time stop |
| `adverse-move` | Price moves hard against us | Enter, exit on the stop loss |
| `already-repriced` | Someone faster moved it before our feed delivered | **Reject** — never chase |
| `thin-book` | Right signal, 10 shares on the offer | **Reject** at the depth gate |
| `kill-switch` | Kill fires with a position open | Flatten and shut down |

A green run proves the machinery: news → match → fair value → gates → risk →
sizing → fill → accounting → exit → journal. **It proves nothing about
profitability** — those price paths were written by hand to exercise code, not
sampled from real markets.

### Then the live path — walk it in order

```bash
python run.py doctor
```
Connectivity, market resolution, sizing feasibility, credentials, kill state.

```bash
python run.py scan "fed"
```
Find live market slugs. Paste them into `config.yaml` and write triggers.

```bash
python run.py watch
```
Read-only. Full pipeline, real books, real news — logs what it *would* trade and
places nothing. **Run this for days before anything else.** It costs nothing and
tells you whether your triggers ever fire and whether markets actually lag.

```bash
python run.py paper
```
Simulated fills against the live book, with latency, partial fills, slippage and
fees. Same code path as live except one object.

```bash
python run.py live
```
Real orders, real money. Requires `.env` credentials and typing `TRADE LIVE`.
Add `--yes` to skip the prompt for headless runs.

```bash
python run.py report      # expectancy, drawdown, fee drag, verdict
python run.py status      # persisted risk state
python run.py kill "..."  # latch the kill switch now
python run.py resume      # clear it after reconciling
```

### Stopping it

Ctrl-C once: the engine flattens every open position, cancels resting orders,
writes a final snapshot, and exits. Ctrl-C twice: immediate abort **without**
flattening — you will have positions open and must reconcile by hand.

This works on Windows too. Letting `KeyboardInterrupt` propagate normally would
tear down the event loop *before* the flatten ran, so the engine installs its
own handler instead.

### If your network blocks Polymarket

Symptom: `doctor` reports the CLOB unreachable, and `curl
https://clob.polymarket.com/ok` fails with a connection reset at TLS handshake
rather than a DNS error. That is network-level blocking, not a bug.

Set a proxy in `config.yaml`:

```yaml
endpoints:
  proxy_url: http://user:pass@host:port
```

It applies to Gamma, the CLOB and the news feeds. The websocket uses it too when
your `websockets` version supports proxying, and logs a warning and connects
directly when it does not. `HTTPS_PROXY` in the environment also works.

### Going live

1. Fund your Polymarket account with USDC on Polygon.
2. `cp .env.example .env` and fill in `POLYMARKET_PRIVATE_KEY` and
   `POLYMARKET_FUNDER_ADDRESS`. **Use a wallet holding only your trading
   bankroll — never your main wallet.** Anyone who reads that file can drain it.
3. `pip install py-clob-client`
4. `python run.py doctor` — confirms the balance is visible.
5. `python run.py live`

Set `execution.fee_bps` from Polymarket's current published fee schedule. It
defaults to 0 and **a wrong fee assumption is the quietest way to turn a
marginal edge negative.**

### Output files

```
logs/decisions.csv        every signal AND every rejection, with reasons
logs/fills.csv            every fill
logs/trades.csv           every closed trade — the input to `report`
logs/events-YYYYMMDD.jsonl  structured log of everything
state/state.json          peak equity, day anchor, trade counts
state/KILL                the latch (exists = stopped)
```

`decisions.csv` is the most valuable file you own. The rejections tell you which
gate is actually binding and whether the edge was ever there.

### Tests

```bash
python -m pytest tests -q
```

No network and no keys required. They cover book maths, phrase matching, every
entry and exit gate, all sizing caps, the daily halt, the drawdown kill, latch
persistence across restarts, portfolio accounting, the fee formula, paper fill
simulation, and two full engine lifecycles driven by the simulator.

If a test in `test_risk_and_portfolio.py` starts failing, stop trading until it
passes.

---

## 5. Testing plan

### How to paper trade properly

1. **`watch` mode for at least a week, live markets, no orders.** You are
   answering one question: do your triggers fire at all, and when they do, has
   the market already moved? Count `already_repriced` rejections in
   `decisions.csv`. If most rejections are that gate, **there is no lag for you
   to trade** and you should stop here.
2. **Then `paper` for 30+ trades minimum.** Do not touch the config mid-run —
   changing thresholds while measuring destroys the sample.
3. **Do not tune on the paper results and then claim the tuned version works.**
   That is fitting to noise. If you change the rules, the sample restarts at
   zero.
4. **Measure your real latency.** Time from headline publication to your
   `seen_ms`. If it is over 60 seconds, the premise is already dead and
   `paper_latency_ms` should be raised to match reality.

### Metrics that matter

Win rate is the least useful number and the one everybody quotes. A strategy
winning 80% of trades and losing 5× on the rest is a slow bankruptcy. `run.py
report` gives you:

- **Expectancy per trade**, in dollars and as % of notional — if this is not
  clearly positive, nothing else matters
- **Profit factor** — gross wins / gross losses; under 1.0 you pay to play
- **Fee drag** — fees as a share of gross P&L; on a 3-5c edge this routinely
  eats everything
- **Max drawdown** — peak-to-trough on the realised curve; this, not the
  average, is what you actually live through
- **t-statistic** — a crude significance check
- **Breakdown by trigger and by exit reason** — which rule is carrying the
  strategy, and which is bleeding

### When to conclude the edge is dead

Stop when any of these is true:

- **Under 30 trades** — you have no sample. No conclusion is available. The
  report says so explicitly and will not give you a positive verdict.
- **Expectancy ≤ 0 over 30+ trades** — stop. The rules as configured lose money.
- **t-stat < 2 with positive expectancy** — indistinguishable from luck. Do not
  scale up.
- **Recent 25-trade window negative while all-time is positive** — the report
  flags this as `deteriorating`. Treat the edge as decaying: halve size or stop.
- **Profit factor < 1.2** — one fee change or a worse fill regime flips it.
- **`already_repriced` dominates your rejections** — someone is consistently
  faster. That is information: you are the liquidity, not the edge.

The kill switch is the automated version of this judgement. Do not raise the
limits because it fired. It fired because it was right.

---

## 6. Honest limitations

### Why this usually fails — market reasons

1. **You are probably not fast enough.** Public RSS is 30s to several minutes
   behind the wire. The window this strategy targets is 30-90s. The arithmetic
   frequently does not work, and no amount of good code fixes a slow input.
2. **Adverse selection.** When your order fills instantly in a fast market, ask
   why someone was so eager to sell. Often the answer is that they know
   something more recent than your headline. The fills you get are
   disproportionately the ones you did not want.
3. **The edge decays by construction.** Every person who automates this idea
   makes the window shorter. Historical performance of a latency strategy has
   very little to say about next month.
4. **Headlines are ambiguous.** "Fed signals possible cut" is not "Fed cuts".
   `none_of` veto lists help and will not save you. A confidently wrong trigger
   fires fast and repeatedly.
5. **Already priced in.** Markets often move on the *expectation* hours before
   the headline. The pre-news anchor detects some of this and not all.
6. **Thin books.** Many Polymarket markets have real depth of a few hundred
   dollars. Your own order moves the price you are trying to capture, and
   getting out is harder than getting in.
7. **Resolution risk you never intended to take.** If you cannot exit, you are
   holding a bet on the actual outcome, plus whatever the resolution criteria
   say in their fine print — which may not match the headline that made you buy.

### Technical risks

- **Websocket drift.** Incremental updates can desync from reality. Mitigated by
  60-second REST resync, crossed-book detection and staleness gates — mitigated,
  not eliminated.
- **API changes.** Undocumented field renames break market resolution silently.
  `doctor` is your canary; run it regularly.
- **Unknown fill state.** A network failure after signing may or may not have
  reached the venue. The code reconciles against trade history and, failing
  that, latches the kill switch rather than guessing. **You must then reconcile
  by hand on Polymarket.**
- **Clock skew.** Every staleness gate depends on your system clock. A drifting
  clock silently widens or closes your windows.
- **Fee assumptions.** `fee_bps` defaults to 0. If the real schedule differs,
  every expectancy number in your report is wrong in the optimistic direction.
- **Paper is optimistic anyway.** It cannot model your own market impact,
  rejections, or the intent of whoever filled you. A profitable paper run is the
  minimum bar for continuing, never evidence of an edge.

### Operational risks

- **Key custody.** `POLYMARKET_PRIVATE_KEY` in a plaintext `.env` on a desktop
  is a real, permanent risk. Use a wallet holding only the bankroll.
- **Regulatory and ToS.** Prediction-market access and automated trading are
  restricted in many jurisdictions. Verify your own position before funding
  anything; the network blocking observed during development is a hint that
  access may not be straightforward where you are.
- **Unattended operation.** The bot flattens on shutdown because nobody is
  watching the screen. If the process dies uncleanly, positions can be left
  open. Check the account after any crash.
- **Automation bias.** The most expensive failure mode is trusting the system
  because it is code. Read `decisions.csv`. Argue with it.
- **It is your money and your decision.** The limits in `config.yaml` only work
  if you leave them alone after a losing streak.
