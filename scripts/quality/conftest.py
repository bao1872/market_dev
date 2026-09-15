"""Pytest conftest for T0's own tests.

The tests live next to the script under ``scripts/quality``. pytest prepends this
directory to ``sys.path`` (prepend import mode), so ``import t0_gate`` resolves
without a PYTHONPATH shim or an in-file ``sys.path`` hack (which would break the
import block under ruff I001).
"""
