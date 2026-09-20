"""Hot wallet for autonomous mode: key file handling, signing, RPC sends, on-chain
reconciliation and the safety rails.

This package and `execution/autonomous.py` are the only places in the project that touch key
material or broadcast a transaction. Paper, dry-run and manual signal modes never import it at
runtime, the read-only dashboard is forbidden from importing it, and the invariant tests keep
signing vocabulary out of every other module.
"""
