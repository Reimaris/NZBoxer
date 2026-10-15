"""Shared lock serializing the orchestrator tick and the title backfill (m65).

The backfill takes it per chunk, so a tick waits at most one chunk (inside its
60 s misfire grace); the tick holds it for its whole run.
"""

import asyncio

library_job_lock = asyncio.Lock()
