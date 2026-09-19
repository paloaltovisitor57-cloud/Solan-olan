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

## PAPER TRADING (start here)

Paper mode uses **live Solana market data**, **no real funds**, **no keys, no signing, no
broadcasting**, and gives every experiment its own isolated database so an old account can never
leak into a freshly requested bankroll.

```bash
cd ~/Solan-olan
git pull
./install-macos.sh              # venv, deps, tests, launcher on PATH, background service (dry-run)
solana-sniper smoke-test        # real-network check of every provider (latency, rate limits, advice)
solana-sniper paper --bankroll-sol 1
```

`paper --bankroll-sol 1` fetches the live SOL/EUR rate once, records it in the session metadata,
converts the bankroll to the EUR accounting currency and starts the full pipeline (discovery →
checks → scoring → sizing → round-trip quote → simulated fill → monitoring → exit → ledger) in the
foreground with a dashboard. The original bankroll is never redefined by later FX moves; the
dashboard additionally shows the *current* SOL equivalent of the equity, labelled as such.
Ctrl+C stops discovery, flushes storage, marks the session ended, finalises outcome tracking,
closes WebSockets/HTTP/SQLite and prints a `SESSION COMPLETE` summary (including storage errors,
dropped writes, provider outages and rate limits; if data integrity was compromised the summary
says so prominently instead of presenting the numbers as trustworthy).

```bash
solana-sniper paper --bankroll-eur 100            # bankroll in EUR instead of SOL
solana-sniper paper --bankroll-sol 1 --name test-a
solana-sniper paper --list                        # experiments in this runtime home
solana-sniper paper --resume <session-id>         # explicit resume; never implicit
solana-sniper paper --allow-fallback-fx ...       # only if no live rate is available; marked as fallback
```

Session ids look like `paper-20260918-132500-1sol-3f9a1c` (or `…-test-a` with `--name`); the
database is `<runtime home>/db/paper/<session-id>.db`, the log
`<runtime home>/logs/paper/<session-id>.log`. Inspect a session (running or finished) with the
global `--paper` option:

```bash
solana-sniper --paper <session-id> status        # portfolio + counts of that experiment
solana-sniper --paper <session-id> positions --all
solana-sniper --paper <session-id> portfolio
solana-sniper --paper <session-id> evaluate      # outcome hit rates of that experiment (§12)
```

Watch a session in the browser (read-only, this Mac only by default):

```bash
solana-sniper dashboard-web                       # http://localhost:8501, picks the running session
solana-sniper dashboard-web --paper <session-id>  # open one paper experiment first
```

See "Web dashboard (read-only)" below for what it shows and what it can never do.

The background service (launchd) remains available and secondary:
`solana-sniper service start|stop|restart|status|logs|install` wrap the scripts below.

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
  web/          read-only Streamlit dashboard: session discovery, read-only SQLite reader, pages
tests/unit, tests/integration, tests/web, tests/fixtures
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
solana-sniper evaluate                      # what happened after each decision (hit rates per score bucket)
solana-sniper paper --bankroll-sol 1        # PAPER: live data, simulated fills, isolated session (start here)
solana-sniper smoke-test                    # real-network provider check with latency and rate-limit advice
solana-sniper service status|start|stop     # background launchd service (wrappers around the scripts)
solana-sniper dashboard-web                 # read-only web dashboard on http://localhost:8501 (§11c)
solana-sniper --home ~/somewhere status     # explicit runtime home; default = the service's platform home
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
milestones, execution_records, state_transitions, errors, outcomes. On startup the account is rebuilt from
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

**One runtime context.** The service, the shell scripts, the installed `solana-sniper` launcher
and the bare venv CLI all resolve the same runtime home: `$SNIPER_HOME` if set, else the platform
default below. `solana-sniper status`, `doctor`, `positions` and `portfolio` therefore show the
installed runtime from any directory, never some `./data` file, and print which home/database
they use. `--home PATH` (or `SNIPER_HOME`) overrides it explicitly; `--config` still selects the
YAML. The installer puts a launcher in `~/.local/bin/solana-sniper` (pinned to the install-time
home) and adds `~/.local/bin` to PATH in `~/.zprofile`/`~/.bash_profile` once (`--no-path` skips).

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
| `./doctor.sh` | verifies python, venv, directories, env file permissions, repository safety (secrets and runtime data git-ignored and untracked, checked with paths relative to the checkout so it is right on a fresh clone with no `data/`), plist validity, launchd state, heartbeat, then runs `solana-sniper doctor` |

**launchd behaviour.** `RunAtLoad` starts the agent at login; `KeepAlive.SuccessfulExit=false`
restarts it after a crash (non-zero exit) with a 10 s throttle but leaves it stopped after
`./stop.sh`; `ExitTimeOut=30` gives the graceful shutdown time to flush; stdout/stderr go to
`logs/service.*.log`. Because it is a *user* agent it runs while your user is logged in; for an
always-on Mac enable automatic login and disable sleep (`sudo pmset -a sleep 0`).

**Crash/restart recovery.** Every fill, position, ledger entry and account snapshot is written
synchronously before the engine continues; on start the portfolio and open positions are rebuilt
from the database, re-subscribed to market data and monitored again (`restored open position …`
appears in the log). Pending confirmations do not survive a restart and are regenerated live.

**Logs.** `./logs.sh` prints the last 50 lines of every log and exits; `./logs.sh -n 100`,
`./logs.sh app|out|err` narrow it; `./logs.sh -f` (or `--follow`) keeps following and says so:
"Following logs. Press Ctrl+C to stop viewing logs; this does not stop the trading service."

**Exactly one process.** `install-macos.sh`/`start.sh` bootstrap the agent once (`RunAtLoad`) and
only `kickstart` (never `-k`) if nothing is running; the earlier `kickstart -k` killed the fresh
process and produced two sessions seconds apart. `./status.sh` shows the launchd service pid, the
engine pid from `state/boot.json`, the number of starts, how the previous run ended
(`clean: <reason>` or `unclean (no stop marker)`) and launchd's last exit code, and warns when
service and engine pids differ.

**Verification status.** The scripts were developed and exercised on Linux with a mocked
`launchctl`/`plutil`/`uname` whose `kickstart` semantics match the real one (full install →
one running instance → status → cmd → stop → restart → doctor → logs flow, plist rendering
validated with `plistlib`), plus `shellcheck` and `bash -n`. They have **not** been run on a
real macOS machine from this environment; `solana-sniper smoke-test`, `./doctor.sh` and
`./status.sh` will show immediately if launchd or a provider is unhappy on your Mac.

## 11b. Entry lifecycle: latch, hysteresis and the entry-attempt audit trail

The first live paper run qualified two tokens and entered neither: qualification was a one-tick
edge trigger (`QUALIFIED` at 67, back to `MONITORING` at 63 one tick later while the quote was
still in flight), the acceleration feature flipped sign purely because a price surge rolled from
the "current" into the "previous" 10-second window, GeckoTerminal-discovered tokens carry no
decimals and a single rate-limited metadata fetch was never retried (so no quote was ever
requested), and nothing durable said why. The lifecycle is now:

```
MONITORING --score >= min_score and checks pass--> QUALIFIED (latched for qualification_latch_s)
   ^                                                 |  decimals -> sizing -> BUY quote -> SELL quote
   |  abandon: score < min_score - hysteresis,       |  -> impact / round-trip loss -> post-quote
   |  hard block (stale data, checks, velocity,      |     validation -> BUY_SIGNAL
   |  sizing zero, round trip not viable),           v
   |  latch expired without a signal             BUY_SIGNAL -> AWAITING_CONFIRMATION -> OPEN
   +---------------- REJECTED on any fatal safety check or liquidity collapse (immediately)
```

* `entry.qualification_hysteresis` (default 8): a latched candidate is abandoned only below
  `min_score - 8`. A 67 -> 63 wobble continues; 67 -> 42 abandons. Hysteresis never bridges a
  hard block, a fatal check, a liquidity collapse, a stale feed or bad round-trip economics.
* `entry.qualification_latch_s` (default 8): the bounded window to finish validation. At expiry
  the attempt is recorded as `EXPIRED` with what was still pending; a candidate that is still
  fully qualified gets another window, at most `max_entry_attempts` per candidate. An abandoned
  window puts the token in `cooldown_after_abandon_s` before it can re-qualify. No candidate
  stays latched forever.
* Post-quote validation checks execution viability (round-trip loss, price impact, sell quote),
  data freshness, fatal checks, hard blocks, sizing and a severe momentum reversal
  (`severe_momentum_60s`); a score inside the hysteresis band does not cancel a viable quote.
* `entry.stale_grace_during_quote_s` (default 5): while a quote is in flight the stale
  transition waits this long beyond `market_data.stale_after_s`; a signal is never generated on
  data older than `stale_after_s`. `market_data.priority_poll_interval_s` refreshes latched,
  signalled and open mints on a faster lane, and throttled DexScreener polls are counted and
  named in the stale reason (`no data for 21s (dexscreener RATE_LIMITED)`) instead of looking
  like a token with no data.
* Mint metadata (decimals, authorities) is retried with a bounded backoff
  (`metadata_retry_s`, `metadata_max_attempts`) instead of once.
* Acceleration is the mean of `acceleration_smoothing_samples` raw samples spaced
  `acceleration_sample_spacing_s` apart: one print moves it by 1/N of its raw swing; a sustained
  reversal still removes the credit after N spacings. Weight and normalisation are unchanged.

**Audit trail.** Every latch window is one `entry_attempts` row (qualified score, features and
checks at qualification, decimals status, sizing, quote attempts/status/error, price impacts,
round-trip loss, post-quote score, score band and hysteresis holds, final decision
`BUY_SIGNAL | ABANDONED | EXPIRED | HARD_REJECT | QUOTE_FAILED | SIZING_ZERO | STALE |
CANCELLED`, block reason, timestamps). Open windows are closed as `CANCELLED` at shutdown.

```bash
solana-sniper --paper <session> entry-attempts        # one row per latch window + decision counts
solana-sniper --paper <session> entry-attempts -v     # full trail per attempt
solana-sniper --paper <session> inspect <MINT>        # attempts first, then the recorded history
```

## 11c. Web dashboard (read-only)

```bash
solana-sniper dashboard-web                          # 127.0.0.1:8501, opens the browser
solana-sniper dashboard-web --paper <session-id>     # start on one paper experiment
solana-sniper dashboard-web --session <session-id>   # start on a live / dry-run session
solana-sniper dashboard-web --port 8600 --refresh-seconds 5 --no-browser
solana-sniper --home ~/somewhere dashboard-web       # another runtime home
```

`dashboard-web` starts a Streamlit page over the recorded sessions of the runtime home. It is a
viewer, nothing else:

* **Read-only by construction.** The web process opens every database with SQLite's `mode=ro`
  URI and `PRAGMA query_only`, on short-lived connections, with a busy timeout and no explicit
  transactions, so it never blocks the engine's WAL writer and the writer never blocks it. It
  runs no `create_all` and no migration: a database older than schema version 4 is shown as
  "unsupported schema, run `solana-sniper migrate`", never migrated from the browser. The page
  has no BUY/SELL/confirm/cancel button, no wallet, no key, no seed phrase, no signing, no
  broadcasting, no config or bankroll editing, no command-file writes and no shell access: the
  only controls are the session selector, the page selector, table filters and a token search
  box. The dashboard process never imports the execution, quote, alert, engine, bootstrap or
  command-file modules (`tests/web/test_boundary.py` checks the import graph and scans the page
  sources for acting widgets and signing vocabulary on every run). It needs no credentials: the
  databases and `<home>/state/status.json` are all it reads.
* **Local only by default.** It binds to `127.0.0.1:8501`. `--host` can bind elsewhere and the
  CLI warns loudly when it does; there is no tunnel, no ngrok, no public relay. To read it on
  your phone, use Tailscale or an SSH port forward to the Mac; the dashboard does no networking
  of its own.
* **Sessions.** PAPER sessions are the databases under `<home>/db/paper/`, LIVE / DRY_RUN /
  REPLAY sessions are the rows of the `sessions` table in `<home>/db/*.db`. The sidebar groups
  them by mode, newest first; the running session (fresh heartbeat naming it) is selected by
  default, `--paper` / `--session` override that.
* **Provenance header on every page.** A live-data paper run shows
  `PAPER · MARKET DATA: LIVE · EXECUTION: SIMULATED · REAL TRANSACTIONS: DISABLED`; a synthetic
  run shows `PAPER / TEST · MARKET DATA: SYNTHETIC`; a live signal session shows
  `LIVE / SIGNAL MODE · EXECUTION: MANUAL SIGNAL / ESTIMATED / USER-REPORTED ·
  REAL TRANSACTIONS: NOT RECONCILED ON-CHAIN`. Market-data provenance is read from the session's
  recorded outcomes, tokens and observations (the heartbeat only when nothing is recorded yet);
  a live-data paper run is never labelled synthetic. Whether the engine is alive comes from the
  heartbeat (`RUNNING` with the engine state, `ENDED`, or `NOT RUNNING` with the last write or
  stop reason).
* **Pages.** Overview (alive, equity, return, drawdown, positions, signals, fills, latest entry
  attempt, provider badges, open positions, latest events; metric cards, no wide tables, phone
  first), Equity (equity / cash / open value curve, drawdown, realized-unrealized-exposure; long
  histories are downsampled inside SQLite and the maximum drawdown is computed from the full
  history), Positions (open and closed, executable vs estimated value, provenance, units,
  staleness, "verified on-chain: no"), Candidates (state, score, age, liquidity, 5-minute volume,
  velocity, momentum, acceleration, data age, check verdict, gate reason; active states
  highlighted), Entry attempts (decision badges BUY_SIGNAL / ABANDONED / EXPIRED / HARD_REJECT /
  QUOTE_FAILED / SIZING_ZERO / STALE / CANCELLED / PENDING and the full forensic record of each
  latch window, filterable by decision), Signals (display only), Fills (simulated vs estimated /
  user-reported, never verified), Token inspector (search by mint or symbol: metadata,
  transition timeline, score history chart, features, checks, quotes, attempts, signals, fills,
  outcomes, price/liquidity chart), Outcomes (the same buckets, Wilson intervals and warnings as
  `solana-sniper evaluate`, with the same "no claim of profitability" wording), Providers
  (governor health from the heartbeat), Engine (heartbeat, ticks, uptime, counters, storage
  queue/dropped/failed, persisted write-integrity with the DATA INTEGRITY COMPROMISED banner),
  Events (transitions, signals, fills, milestones, errors; filters ALL / TRADING / DATA /
  PROVIDERS / ERRORS).
* **Refresh.** Auto-refresh every 2–30 s (default 3, `--refresh-seconds`, adjustable in the
  sidebar) through a Streamlit fragment; queries are cached for two seconds per viewer so a
  running session is never shown frozen. A transient `SQLITE_BUSY` is retried and then shown as
  "Database busy — retrying" while the last good data stays on screen.
* **Secrets.** Every free-text field (error messages, quote errors, provider errors, exception
  messages) passes through the same redaction as the logs before it reaches the page.

The dashboard needs the `web` extra (`streamlit`, `plotly`, `pandas`); `install-macos.sh` and
`update.sh` install it. Without it, `dashboard-web` exits with the install hint.

## 12. Outcome measurement (`evaluate`)

The engine cannot know which token will go up. What it can do is record, for every candidate it
saw, what happened next, and let you check whether its scoring has any edge at all before you
trust it with a single confirmation. That is what `OutcomeTracker` (`strategy/outcomes.py`) and
`solana-sniper evaluate` (`strategy/evaluation.py`) do.

* From the first priced snapshot of every candidate (rejected ones included, so there is no
  survivorship bias) the tracker follows the market for `outcomes.horizon_s` (default 1 h) and
  then writes one `outcomes` row: peak multiple and time to peak, worst drawdown after the peak,
  final multiple, whether liquidity fell by more than `rug_liquidity_drop_pct` from its high,
  the best score the engine gave it, and what the engine did (qualified, BUY signalled, entered,
  closed PnL if the exit happened within the horizon). Data that goes silent for
  `silence_timeout_s` finalises early (the pool died or the provider dropped it). At most
  `max_followed` tokens are followed at once; the market watch is kept alive until measurement
  ends, then released.
* On shutdown, rows still in flight are persisted with `truncated: true`; `evaluate` excludes
  them unless `--include-truncated` is given.
* Every row carries two provenances: `market_data` (`LIVE` real Solana observations, `SYNTHETIC`
  the offline world, `UNKNOWN_LEGACY` before schema version 4) and `execution` (`SIMULATED` for
  paper/dry-run, `MANUAL_SIGNAL` for live signal mode). A paper session is `LIVE` market data
  with `SIMULATED` execution and is never described as synthetic; its multiples are still
  passive price paths, not executable returns.
* `evaluate` prints, per group (all followed, rejected, never qualified, qualified, signalled,
  entered, and score buckets <40 / 40–59 / 60–74 / 75–89 / 90+): n, share that reached ≥2x, ≥5x,
  ≥10x with 95% Wilson intervals, median peak, median end, median drawdown, liquidity-pull rate,
  median closed PnL. Groups with n < 30 are dimmed because their rates are noise.

Read the numbers with these caveats, which the command also prints:

* Multiples are what a passive observer saw from the first snapshot. Nobody could have bought
  at that price for that size; slippage, fees, failed fills and the round-trip haircut are not
  included. A bucket "reaching 5x" is not a return.
* Dry-run and synthetic rows are labelled `simulated` and say nothing about real Solana tokens.
  Use `--session` to separate a live-data session from a synthetic one.
* Past outcomes on a few hundred tokens are a weak, noisy signal about a market that changes
  weekly. `evaluate` is there to falsify the scorer, not to tune it to the past. It never
  feeds back into the engine automatically.

```bash
solana-sniper run --dry-run                 # collect live-data outcomes for a day or more
solana-sniper evaluate                      # then look at what the scores were actually worth
solana-sniper evaluate --session <ID> --min-observations 10
```

## 13. Reliability: storage integrity and provider governance

**Storage.** Every write has a record class. CRITICAL rows (fills, positions, ledger, account
state, sessions, outcomes) are committed synchronously at their boundary and never queued.
IMPORTANT rows (tokens, token state, signals, decisions, execution records, milestones) go
through the background writer but are never dropped. TELEMETRY rows (observations, trades,
features, checks, scores, quotes, portfolio snapshots, transitions, errors) are research data:
they are dropped only when the writer is more than `storage.max_queued_telemetry` rows behind,
and every drop is counted per kind, logged once per kind per minute, exposed in the heartbeat
(`database.integrity`), turns health to `DEGRADED`, is persisted per session in
`session_integrity`, and makes `evaluate` print `INCOMPLETE DATA` for that session. Batches are
serialised, token rows use `INSERT … ON CONFLICT(mint) DO UPDATE` (first sighting keeps its
provenance, later sightings fill missing metadata, `final_state` is never wiped), session rows
upsert so an explicit resume works, a batch the writer already dequeued is committed even if
shutdown cancels it, and a row that cannot be built or serialised is counted as a failed write of
its kind instead of vanishing. SQLite opens with a 30 s busy timeout so `status` from another
terminal never turns into "database is locked".

**Providers.** Every HTTP request passes through the provider governor (`infra/governor.py`):
per-host pacing (token bucket), concurrency cap, bounded waiting queue, cooldown on HTTP 429
(Retry-After when supplied, else exponential backoff with jitter that resets on success),
fast-fail while a cooldown or open circuit is longer than the caller should wait, circuit breaker
after repeated failures with a single half-open probe, and a health state per provider
(`HEALTHY / RATE_LIMITED / DEGRADED / DOWN`) with counters for rate limits, retries, backoffs,
fast fails, trips and recoveries. Rate limits are summarised (first occurrence, then one line
per minute with the count) instead of one warning per request. A rate-limited optional
enrichment (mint authorities, holder distribution) leaves the affected checks `UNKNOWN`, never
`PASS`, is counted as `checks_degraded`, and is not an engine error. Identical RPC calls are
coalesced and cached briefly. Defaults are in `configs/default.yaml` under
`providers.rate_limits`; with a Helius RPC URL the `helius` limits apply.

**Health states.** `HEALTHY` (everything works, data complete), `DEGRADED` (running, but
research rows were dropped or a provider is rate limited / tripped), `UNHEALTHY` (engine not
ticking, nothing connected, or database writes failing), `STOPPED`. `solana-sniper health`
exits 0 only for HEALTHY.

## 14. Quality gates

```bash
pytest            # unit + integration (synthetic end-to-end, invariants, recovery) + web dashboard
ruff check .
mypy              # --strict via pyproject
```

## 15. Limitations (honest list)

* Live provider connectivity could **not** be exercised from the build sandbox (all external hosts
  were blocked by its egress policy). The first real Mac run exposed defects the mocked suite had
  missed (duplicate-mint UNIQUE errors, silent write drops, RPC/GeckoTerminal 429 storms, a false
  `.gitignore` failure, a Rich-wrapped path assertion, a CLI that ignored the service's runtime
  home, a log viewer that never returned, and a double service start); each now has a regression
  test that failed before the fix. `solana-sniper smoke-test` is the real-network check to run
  on the Mac before a paper session. The adapters are written against the documented API shapes and
  tested with fixtures, and the *live composition* (PumpPortal WS + GeckoTerminal + DexScreener +
  Solana RPC + Jupiter + CoinGecko) is driven end to end in `tests/integration/test_live_pipeline_mocked.py`
  with the transports mocked. Real endpoints may still differ in detail; `solana-sniper doctor`
  will tell you within seconds which provider fails.
* Outcome rows are observational. They cannot say what a trade would have made, only how
  prices and liquidity moved after the engine saw a token; a position that closes after the
  outcome horizon has no `closed_pnl_pct` on its row. The sample a single machine collects is
  small and the market is non-stationary, so `evaluate` is a falsification tool, not a
  strategy optimiser, and the engine never adjusts itself from it.
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
