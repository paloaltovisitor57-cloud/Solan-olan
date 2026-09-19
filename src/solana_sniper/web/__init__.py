"""Read-only web dashboard (Streamlit).

Boundary: this package only ever *reads*. It opens the SQLite databases with `mode=ro`, never
runs `create_all` or a migration, never imports the execution, quote, alert or command-file
modules, and renders no control that could confirm, sign, broadcast or change a setting. It
needs no credentials: the databases and the heartbeat file are all it looks at.
"""

from __future__ import annotations

FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "solana_sniper.execution",
    "solana_sniper.wallet",
    "solana_sniper.quotes",
    "solana_sniper.alerts",
    "solana_sniper.app.engine",
    "solana_sniper.app.bootstrap",
    "solana_sniper.app.command_file",
    "solana_sniper.app.smoke",
    "solana_sniper.cli.commands",
)
"""Modules the dashboard process must never load (checked by the test-suite)."""
