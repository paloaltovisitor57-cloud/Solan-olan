# solana-sniper

Live Solana new-token trading engine in Python 3.12: continuous discovery of freshly launched
tokens, real-time market data, token checks, short-horizon features, 0–100 entry scoring,
dynamic bankroll-based position sizing, executable round-trip quotes, **manual-confirm** BUY/SELL
signals, tick-by-tick position monitoring with adaptive trailing exits, an auditable ledger, a
terminal dashboard, SQLite persistence with restart recovery, replay and a `doctor` command.

> **Execution boundary.** The engine never asks for, stores or uses a private key, never signs and
> never broadcasts a transaction. Every real-money action is a *recommendation* that a human confirms
> with `b N` / `s N`. Where enabled, it can prepare an **unsigned** Jupiter swap transaction for the
> configured **public** key so the user can sign it in their own wallet. Invariant tests grep the
> source tree for signing/broadcast code paths and fail if any appear.
>
> **Provenance of every figure.** Nothing this software records is reconciled against the chain.
> Each fill, position and ledger entry carries a `provenance`: `SIMULATED` (dry-run),
> `ESTIMATED` (a human confirmed and the amounts were taken from the quote), `USER_REPORTED` (the
> human typed the amounts / a signature string they saw in their wallet) or `UNKNOWN_LEGACY`
> (recorded before provenance existed). A reported signature is stored verbatim and is **not**
> treated as verification; `verified_onchain` is always `false`. Dashboard, `positions`,
> `portfolio`, `status` and the heartbeat all say so.
>
> **Software tests are not investment evidence.** The test suite proves that the pipeline, the
> accounting and the safety invariants behave as specified on synthetic and mocked data. Equity
> figures produced in dry-run or against the synthetic world (which is deliberately generous) say
> nothing about real returns, and this repository makes no performance claim.

---

## 1. Architecture

```
 PumpPortal WS ─┐                                ┌─ Solana RPC (mint authorities, holders)
 GeckoTerminal ─┼─ DiscoveryService ─┐           │
 DexScreener  ──┘  (dedupe, age gate) │          │
                                      ▼          ▼
 DexScreener batch poll ─┐   ┌──────────── Engine.tick() every 250 ms ────────────────┐
 PumpPortal trades  ─────┼─▶ │ TokenTracker (rolling buffers, dedupe, stale detection) │
 (Synthetic world)  ─────┘   │  → FeatureEngine → TokenChecker → EntryScorer → Gate   │
                             │  → RiskEngine (size) → RoundTripEvaluator (Jupiter)    │
                             │  → BuySignal → ExecutionInterface (manual / dry-run)   │
                             │  → PortfolioAccount (ledger) → PositionMonitor         │
                             │  → ExitEngine (adaptive trailing, momentum, liquidity, │
                             │     volume, max loss, timeout, abnormal, stale)        │
                             │  → SellSignal → ExecutionInterface → ledger update     │
                             └──────────────────┬───────────────────────────────────┘
                                                │ EventBus (bounded per-subscriber queues)
                    ┌───────────────────────────┼─────────────────────────────┐
                    ▼                           ▼                             ▼
          PersistenceSubscriber          AlertService                   Dashboard / CLI
          (SQLite, batched writer)  (terminal, Discord, Telegram)    (rich TUI, b/s/r/i/q)
```

Key design points:

* **One engine, three modes.** `run` (live signal mode, human confirms), `run --dry-run` (same
  live data and logic, confirmations simulated after a delay with a pessimistic fill haircut) and
  `replay SESSION_ID` (recorded observations on a manual clock). Only the `ExecutionInterface`
  adapter and data sources differ; the engine code is identical.
* **Explicit state machine** per candidate: `DISCOVERED → MONITORING → QUALIFIED → BUY_SIGNAL →
  AWAITING_CONFIRMATION → OPEN → EXIT_SIGNAL → AWAITING_EXIT_CONFIRMATION → CLOSED`, plus
  `REJECTED`, `EXPIRED`, `DATA_STALE`, `SIGNAL_CANCELLED`. Transitions are table-driven and guarded;
  a BUY signal can only be created from `QUALIFIED`, so duplicate simultaneous entries are impossible.
* **Executable, not displayed, prices.** Before any BUY signal the engine obtains a BUY quote and a
  SELL quote for the tokens it would receive (round-trip test). Open positions are valued from a
  refreshed exit quote whenever one is fresh; otherwise the displayed price is used and flagged.
* **Never silently exceeds cash.** The ledger refuses any entry that would make cash negative; sizing
  is capped by profile limits, hard limits, total exposure, pool liquidity and available cash.
* **Stale data suppresses signals.** Candidates move to `DATA_STALE`; pending BUY signals are
  cancelled; open positions raise a `DATA_STALE` exit signal after a configurable time.
* **Hot path is non-blocking.** Provider I/O runs in bounded tasks; storage writes go through a
  batched background writer; only fills/positions/ledger/account state are awaited so an open
  position survives a crash immediately after it opens.

## 2. Repository tree

```
src/solana_sniper/
  app/          bootstrap (composition root), engine, event bus, persistence subscriber, replay, doctor
  cli/          typer CLI, rich dashboard, stdin command handler (b N / s N / r N / i N / q)
  config/       pydantic settings (YAML + env) — every trading parameter lives here
  domain/       models, enums, Decimal money helpers, clock, state machine, events
  discovery/    TokenDiscoveryProvider + PumpPortal (WS), GeckoTerminal, DexScreener, service
  market_data/  MarketDataProvider + DexScreener batch, PumpPortal trade stream, tracker, synthetic world
  token_analysis/ TokenMetadataProvider / LiquidityProvider over Solana JSON-RPC (+Helius DAS)
  features/     rolling short-horizon feature engine
  filters/      token checks → PASS / REJECT / UNKNOWN (fatal vs non-fatal)
  strategy/     entry scorer (0–100) and qualification gate
  risk/         dynamic sizing engine, bankroll tiers, profiles, hard limits, milestones
  quotes/       QuoteProvider + Jupiter adapter + round-trip evaluator
  execution/    ExecutionInterface, manual + dry-run executors, unsigned-tx preparer
  positions/    position monitor, adaptive trailing, exit engine
  portfolio/    ledger accounting, FX (CoinGecko / static)
  alerts/       AlertProvider: terminal, Discord, Telegram
  storage/      SQLAlchemy async models, repository, serialization
  telemetry/    structlog logging, metrics, pipeline latency timers
  infra/        rate-limited HTTP client, reconnecting WebSocket, backoff
tests/unit, tests/integration, tests/fixtures
configs/default.yaml   configs/synthetic.yaml
```

## 3. Installation

```bash
git clone <this repo> && cd Solan-olan
uv venv --python 3.12 .venv && source .venv/bin/activate      # or: python3.12 -m venv .venv
uv pip install -e ".[dev]"                                      # or: pip install -e ".[dev]"
cp .env.example .env                                            # optional: keys / RPC URLs
solana-sniper doctor                                            # verify config, db, network, providers
```

## 4. Environment variables

All optional. Prefix `SNIPER_`, nesting with `__`. Any YAML key can be overridden the same way
(e.g. `SNIPER_RISK__PROFILE=AGGRESSIVE`).

| Variable | Purpose |
|---|---|
| `SNIPER_PROVIDERS__SOLANA_RPC_URL` | JSON-RPC HTTP endpoint (default public mainnet; Helius/QuickNode URL recommended) |
| `SNIPER_PROVIDERS__SOLANA_WS_URL` | RPC WebSocket (reserved for a future logs-subscribe discovery adapter) |
| `SNIPER_PROVIDERS__HELIUS_API_KEY` | Enables DAS `getTokenAccounts` holder counts |
| `SNIPER_PROVIDERS__JUPITER_API_KEY` | Uses `api.jup.ag` with higher rate limits; without it `lite-api.jup.ag` is used |
| `SNIPER_PROVIDERS__WALLET_PUBLIC_KEY` | **Public** key only; enables unsigned swap preparation. Anything that looks like a secret is rejected at config load |
| `SNIPER_ALERTS__DISCORD_WEBHOOK_URL` | Push alerts (HIGH/URGENT by default) |
| `SNIPER_ALERTS__TELEGRAM_BOT_TOKEN` / `SNIPER_ALERTS__TELEGRAM_CHAT_ID` | Push alerts |
| `SNIPER_CONFIG` | Path of the YAML config (default `configs/default.yaml`) |

Configuration is strict: unknown YAML keys (including nested typos such as `risk.profil`) and
unknown `SNIPER_*` variables in the environment or dotenv files abort startup with the offending
key path; numbers must be finite and within documented bounds; validation errors never echo the
supplied value. Variables that do not start with `SNIPER_` are never inspected. The service
variables `SNIPER_SERVICE_MODE`, `SNIPER_HOME`, `SNIPER_VENV`, `SNIPER_PYTHON`,
`SNIPER_SERVICE_LABEL` and `SNIPER_LAUNCH_AGENTS_DIR` are reserved for the deployment scripts.

Credentials never reach logs or diagnostics: every configured secret and every URL-embedded
credential (Helius `api-key`, Telegram bot token, Discord webhook token, userinfo) is registered
with a redaction registry at load time; HTTP errors carry only method, sanitised endpoint and
status (never query strings, headers or response bodies); structlog events, standard-library
records (including handlers added by third parties) and tracebacks are scrubbed before they are
written. `wallet_public_key` must decode from base58 to exactly 32 bytes; anything else is rejected
without being echoed.

## 5. Providers

| Provider | Used for | Key | Notes |
|---|---|---|---|
| PumpPortal `wss://pumpportal.fun/api/data` | new pump.fun token creations (`subscribeNewToken`), per-token trade stream (`subscribeTokenTrade`) | none | one shared socket, auto-resubscribe on reconnect |
| GeckoTerminal `/networks/solana/new_pools` | new pools across Raydium/PumpSwap/Meteora/Orca with `pool_created_at` | none | 30 req/min, polled |
| DexScreener `/tokens/v1/solana/{mints}` | batch market data (price, liquidity, volume, txns, mcap/fdv) 30 mints/request | none | 300 req/min; also `token-profiles`/`token-boosts` as optional discovery |
| Solana JSON-RPC | `getAccountInfo` (mint/freeze authority, Token-2022 extensions), `getTokenLargestAccounts` (concentration) | optional | public RPC is rate-limited; Helius adds holder counts |
| Jupiter `/swap/v1/quote`, `/swap/v1/swap` | executable quotes, round-trip test, exit valuation, **unsigned** tx | optional | never signed/broadcast by this software |
| CoinGecko `/simple/price` | SOL→EUR, USD→EUR | none | static fallback in config |

Configure sources in YAML: `discovery.sources`, `market_data.sources`, `quotes.source`.
`synthetic` swaps every provider for a deterministic constant-product launch simulator (used by the
offline end-to-end run and the integration tests).

## 6. Configuration example

`configs/default.yaml` is the reference (every category: discovery, market_data, filters, entry,
risk, exit, quotes, portfolio, alerts, storage, telemetry, dry_run, dashboard). A minimal override:

```yaml
discovery: { sources: [pumpportal, geckoterminal], max_token_age_s: 1800 }
filters:   { min_liquidity_usd: 3000, max_top10_holder_pct: 0.45, require_mint_authority_revoked: true }
entry:     { min_score: 65, signal_ttl_s: 45, cooldown_after_exit_s: 300 }
risk:
  profile: EXTREME
  starting_bankroll_eur: 50
  profiles:
    EXTREME: { base_fraction: 0.50, max_fraction: 0.90, max_total_exposure_fraction: 0.95, max_open_positions: 2 }
  hard_limits: { max_single_position_fraction: 0.95, min_position_eur: 5, max_fraction_of_pool_liquidity: 0.03 }
  tiers:
    - { name: micro, min_equity_eur: 0,    fraction_multiplier: 1.00 }
    - { name: small, min_equity_eur: 300,  fraction_multiplier: 0.85 }
    - { name: mid,   min_equity_eur: 1500, fraction_multiplier: 0.65 }
  milestones_eur: [50, 100, 150, 300, 700, 1500, 3000, 5000, 10000, 25000, 50000, 100000, 300000]
exit:
  trailing:
    tiers: [{min_multiple: 0, drawdown_pct: 0.30}, {min_multiple: 1.8, drawdown_pct: 0.18}, {min_multiple: 5, drawdown_pct: 0.10}]
  max_loss_pct: 0.35
  max_holding_s: 1800
quotes: { source: jupiter, slippage_bps: 300, max_quote_age_s: 8, prepare_unsigned_transaction: false }
```

## 7. Commands

```bash
solana-sniper run --dry-run                 # live Solana data, simulated confirmations (start here)
solana-sniper run                           # live SIGNAL mode: confirm with b N / s N in the TUI
solana-sniper run --dry-run -c configs/synthetic.yaml   # offline end-to-end run (no network)
solana-sniper run --no-dashboard --duration 300         # plain log output, stop after 5 minutes

solana-sniper status                        # portfolio snapshot + db counts (from another terminal)
solana-sniper positions [--all]
solana-sniper candidates
solana-sniper portfolio                     # ledger view
solana-sniper inspect <MINT>                # transitions, checks, scores, signals for a token
solana-sniper sessions                      # recorded sessions
solana-sniper replay <SESSION_ID>           # re-run recorded observations through the engine
solana-sniper doctor                        # config, db, network, providers, credentials, quote test
```

Dashboard keys (type and press Enter): `b 1` confirm BUY #1 · `r 1` reject · `s 2` confirm SELL #2 ·
`i 2` ignore · `b 1 0.12 950000 <sig>` confirm and record the actual SOL spent / tokens received ·
`p` positions · `c` candidates · `q` quit.

## 8. Bankroll scaling

`RiskEngine.size()` (see `risk/engine.py`):

```
size = equity × base_fraction(profile) × tier_multiplier(equity) × confidence(score)
       × drawdown_multiplier × streak_multiplier × slippage_multiplier
```

then capped in order by: profile `max_fraction`, hard `max_single_position_fraction`, remaining
total-exposure room, `max_fraction_of_pool_liquidity × pool liquidity`, hard `max_position_eur`,
**available cash**. Below `min_position_eur` → no trade. The quoted spend is what the signal shows.

* **Profiles** NORMAL / AGGRESSIVE / EXTREME set base fraction, max fraction, exposure and max
  open positions. EXTREME starts at 50 % of equity per trade, capped at 90 %, still obeying hard limits.
* **Tiers** shrink the fraction as equity grows (micro 1.0 → whale 0.2) and can change max positions.
* **Confidence** scales 0.5→1.0 between `confidence_min_score` and `confidence_full_score`.
* **Drawdown** scales linearly from 1.0 at 15 % drawdown down to 0.35 at 50 %.
* **Streak** −15 % per consecutive loss (floor 0.4), +8 % per consecutive win (cap 1.25), ×0.75
  when recent expectancy is negative.
* **Slippage** penalises entry+exit impact between 300 and 1000 bps (floor 0.5).
* **Milestones** are recorded on every crossing (with 5 % hysteresis on the way down) and persisted.

Because the size is recomputed from *current* equity on every signal, a €50 bankroll that grows
to €300 automatically moves to the `small` tier and sizes from the new equity.

## 9. Entry scoring

`EntryScorer` (see `strategy/scoring.py`) produces 0–100 from weighted, normalised components
(weights in `entry.weights`): freshness (exponential decay, half-life 8 min), liquidity growth
(60 s), trade velocity (tx/min), participation growth (unique traders / holders), momentum
(blend of 30 s and 60 s), acceleration (10 s return vs previous 10 s), demand balance (bell around
`ideal_buy_ratio` 72 % buys), holder concentration, exit viability (round-trip loss) and entry
slippage. Multipliers: ×0.7 when >25 % below the local peak, ×0 when data is stale or a fatal check
fails. The `EntryGate` then requires: no fatal check, not stale, `min_observations`, all non-fatal
checks passing (UNKNOWNs per `filters.unknown_policy`), `min_score`, momentum and velocity floors.

Token checks are recorded individually as PASS / REJECT / UNKNOWN. Fatal rejects end the candidate
(mint/freeze authority present, transfer fee/hook, non-transferable, permanent delegate, token too
old, liquidity pulled ≥40 %, catastrophic round trip). Non-fatal rejects only block qualification.

## 10. Exit logic

`ExitEngine` (see `positions/exit_engine.py`) evaluates on every tick, most urgent first:

1. `DATA_STALE` (HIGH) after `stale_exit_after_s` without data.
2. `ABNORMAL_EVENT` (URGENT): 10 s price spike ≥ 300 % or spread ≥ 2000 bps.
3. `LIQUIDITY_COLLAPSE` (URGENT): liquidity −35 % within 60 s.
4. `MAX_LOSS` (HIGH): PnL ≤ −35 %.
5. `TRAILING_PEAK`: executable value ≥ adaptive threshold below the post-entry peak.
6. `MOMENTUM_DETERIORATION`: 30 s momentum ≤ −12 % after ≥ 10 % peak gain.
7. `VOLUME_COLLAPSE`: activity ≤ 15 % of the position's peak velocity after 90 s.
8. `TIMEOUT`: holding time ≥ 30 min.

The adaptive trailing threshold starts from a tier by peak multiple (30 % → 24 % → 18 % → 14 % →
10 % → 7 % at 1.0×/1.3×/1.8×/3×/5×/10×), widens with volatility (≤1.5×), tightens on low
liquidity (×0.7), negative momentum (×0.8) and long holding (×0.85), clamped to [4 %, 50 %].
Exit signals have a TTL and a cooldown; an ignored exit returns the position to `OPEN`.

## 11. Persistence, recovery, replay

SQLite (WAL) tables: sessions, tokens, observations, trades, features, check_results, scores,
signals, decisions, quotes, positions, fills, ledger, account_state, portfolio_snapshots,
milestones, execution_records, state_transitions, errors. On startup the account is rebuilt from
`account_state` (or from the ledger if missing), open positions are re-registered, subscribed to
market data and monitored again. `replay SESSION_ID` feeds the recorded stream into a fresh
engine on a manual clock and writes to `<db>-replay.db`.

## MACOS PLUG-AND-PLAY SETUP

One-time setup on an always-on Mac; afterwards the engine runs as a launchd user agent that
survives closed Terminals, restarts after crashes and needs no Claude Code session.

```bash
git clone <repo-url> Solan-olan
cd Solan-olan
./install-macos.sh
```

`install-macos.sh` verifies Python ≥ 3.12 (`brew install python@3.12` if missing), creates
`.venv`, installs dependencies, creates the runtime home, validates the configuration, applies
database migrations, runs the test suite, renders and installs
`~/Library/LaunchAgents/com.solanasniper.agent.plist`, bootstraps it and waits for a healthy
heartbeat. It is safe to re-run.

**Runtime state lives outside the repository** (never committed):

```
~/Library/Application Support/SolanaSniper/
  db/         sniper.db (SQLite, WAL)            — portfolio, positions, ledger, observations
  logs/       sniper.log (rotating), service.out.log, service.err.log
  state/      status.json (heartbeat, every 5 s), commands (headless confirmations)
  sniper.env  local secrets/config (mode 600): API keys, SNIPER_SERVICE_MODE, overrides
```

`SNIPER_HOME` points there for the service and every script; `data/`, `.env` and `sniper.env`
are git-ignored. Put API keys (Helius, Jupiter, Discord/Telegram, wallet **public** key) in
`sniper.env`; the format is the same as `.env.example`.

**Deployment mode.** The service runs `solana-sniper run --dry-run --no-dashboard --quiet` by
default: live Solana data, real signals, simulated confirmations. Set `SNIPER_SERVICE_MODE=signal`
in `sniper.env` (or `./install-macos.sh --mode signal`) for live signal mode, where *you* confirm
each BUY/SELL: `./cmd.sh b 1`, `./cmd.sh s 2`, `./cmd.sh r 1`, `./cmd.sh i 2`, or
`./cmd.sh b 1 0.12 950000 <txsig>` to record the actual fill you executed in your own wallet.
There is no mode that signs or broadcasts transactions.

**Commands**

| Script | What it does |
|---|---|
| `./start.sh` | (re)render the plist, bootstrap and kickstart the agent, wait for a healthy heartbeat |
| `./stop.sh` | `launchctl bootout` → SIGTERM → the engine flushes storage, portfolio state, open positions, logs |
| `./restart.sh` | stop + start; open positions are restored from the database and monitoring resumes |
| `./status.sh` | launchd state + PID, uptime, connection status per provider, market-data freshness, database health, tokens monitored, open positions, last signal, last error |
| `./logs.sh [-n N] [app\|out\|err\|all]` | follow the rotating app log and launchd stdout/stderr |
| `./cmd.sh <command>` | queue a confirmation for the headless service (`b N`, `s N`, `r N`, `i N`, `p`, `c`) |
| `./update.sh` | fast-forward pull, reinstall deps, `config-check`, `migrate`, run tests; restarts the service **only** if every step passes |
| `./doctor.sh` | verifies python, venv, directories, env file permissions, git-ignore, plist validity, launchd state, heartbeat, then runs `solana-sniper doctor` (config, db, network, providers, quotes) |

**launchd behaviour.** `RunAtLoad` starts the agent at login; `KeepAlive.SuccessfulExit=false`
restarts it after a crash (non-zero exit) with a 10 s throttle but leaves it stopped after
`./stop.sh`; `ExitTimeOut=30` gives the graceful shutdown time to flush; stdout/stderr go to
`logs/service.*.log`. Because it is a *user* agent it runs while your user is logged in; for an
always-on Mac enable automatic login and disable sleep (`sudo pmset -a sleep 0`).

**Crash/restart recovery.** Every fill, position, ledger entry and account snapshot is written
synchronously before the engine continues; on start the portfolio and open positions are rebuilt
from the database, re-subscribed to market data and monitored again (`restored open position …`
appears in the log). Pending confirmations do not survive a restart and are regenerated live.

**Verification status.** The scripts were developed and exercised on Linux with a mocked
`launchctl`/`plutil`/`uname` (full install → start → status → cmd → stop → restart flow, plist
rendering validated with `plistlib`), plus `shellcheck` and `bash -n`. They have **not** been run
on a real macOS machine from this environment; `./doctor.sh` and `./status.sh` will show
immediately if launchd or a provider is unhappy on your Mac.

## 12. Quality gates

```bash
pytest            # unit + integration (synthetic end-to-end, invariants, recovery)
ruff check .
mypy              # --strict via pyproject
```

## 13. Limitations (honest list)

* Live provider connectivity could **not** be exercised from the build sandbox (all external hosts
  were blocked by its egress policy). The adapters are written against the documented API shapes and
  tested with fixtures, and the *live composition* (PumpPortal WS + GeckoTerminal + DexScreener +
  Solana RPC + Jupiter + CoinGecko) is driven end to end in `tests/integration/test_live_pipeline_mocked.py`
  with the transports mocked. Real endpoints may still differ in detail; `solana-sniper doctor`
  will tell you within seconds which provider fails.
* Holder counts require Helius; on public RPC the holder-count check is UNKNOWN (concentration from
  `getTokenLargestAccounts` still works).
* DexScreener does not expose a "new pairs" endpoint; PumpPortal + GeckoTerminal are the
  low-latency discovery paths. A Solana `logsSubscribe` adapter (Raydium/PumpSwap pool creation)
  is the natural next addition behind `TokenDiscoveryProvider`.
* Fills in live signal mode are booked at quoted amounts (`ESTIMATED`) unless the user reports
  actual amounts (`b 1 <sol> <tokens_ui> <sig>`, recorded as `USER_REPORTED`); there is no
  on-chain fill reconciliation, so no record is ever `VERIFIED_ONCHAIN`.
* Databases written before schema version 2 are migrated on first start: legacy fills, positions
  and ledger rows are labelled `UNKNOWN_LEGACY` and legacy fill token amounts are kept under
  `legacy_token_amount` because their unit was ambiguous (buys were UI, sells were raw). A legacy
  record that said `simulated: true` keeps that evidence as `SIMULATED` provenance plus
  `legacy_simulated: true`; schema version 3 repairs databases that the first version of the v2
  migration had left as `UNKNOWN_LEGACY` + `simulated`. Legacy open positions are still restored
  and valued from the displayed price, but they are flagged `units?` and are not re-quoted,
  because their decimals are unverified.
* Log redaction boundaries: structlog events (key-aware, nested mappings), standard-library
  records created through `Logger.makeRecord` (message, `args`, `extra` attributes and traceback
  text, including handlers registered before the app configured logging) and exception chains
  as the traceback module would display them. Not covered: records built by code that bypasses
  `Logger.makeRecord`, attributes a custom formatter reads from sources other than the record,
  and output written outside the logging module (for example `print`).
* The synthetic world is deliberately generous (runners pump several ×). It validates plumbing,
  not strategy profitability.
