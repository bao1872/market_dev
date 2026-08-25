"""DEVELOPMENT WRAPPER ONLY — production logic lives in app/services.

This thin entrypoint is intentionally left here for host-side developer
workflows (``.venv/bin/python scripts/review_historical_ew_db_shadow.py``).
It simply delegates to
:func:`app.services.review_historical_ew_db_shadow_runner.main`, which is
the canonical owner whose source code **also** ships inside the backend
container (Live Mount includes ``backend/app``).

The formal production execution path is a one-shot backend container:

    python -m app.services.review_historical_ew_db_shadow_runner

NOT this file, because the host process is not bound by the backend
container's ``memory.max=4GiB`` cgroup contract.  Running this script on
the host will FAIL CLOSED on the ``production_cgroup_4g_confirmed`` gate
until the exact 4 GiB cgroup is observed.
"""
from __future__ import annotations

import os
import sys

# Allow the caller to do ``PYTHONPATH=. python scripts/...`` from the
# backend project root.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


if __name__ == "__main__":
    from app.services.review_historical_ew_db_shadow_runner import main

    sys.exit(main())
