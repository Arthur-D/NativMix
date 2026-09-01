"""Pure, Qt-free remote synchronization core for NativMix (Layer 1).

This package defines a self-contained, standard-library-only foundation for
synchronizing a receiver-owned view of mixer state to a remote peer:

* :mod:`nativmix.remote_sync.schema` — canonical wire schema, normalization,
  limits, and a deterministic content hash. No persistence API.
* :mod:`nativmix.remote_sync.state` — epoch/revision clock, publication
  contract, sender-side gap/hash detection, idempotency and pending-command
  bookkeeping.
* :mod:`nativmix.remote_sync.protocol` — strict typed messages, canonical
  JSON codec, and 4-byte length-prefixed framing.
* :mod:`nativmix.remote_sync.transport` — nonblocking, poll-driven TCP
  transport (server and client) with heartbeats, inactivity detection, and
  jittered reconnect backoff.

Nothing in this package imports Qt, touches hardware, or performs file I/O.
It is designed to be consumed later by ``hardware/remote_midi.py`` without
requiring any changes to this package's public API.
"""

from __future__ import annotations

from nativmix.remote_sync.protocol import PROTOCOL_VERSION
from nativmix.remote_sync.schema import SCHEMA_VERSION

__all__ = ["PROTOCOL_VERSION", "SCHEMA_VERSION"]
