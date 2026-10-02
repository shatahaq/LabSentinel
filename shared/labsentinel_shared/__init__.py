"""Lab Sentinel shared layer.

Transport-agnostic, Windows-agnostic building blocks shared by the agent and the
dashboard: the versioned wire protocol, rule/config data models, and the
whitelist matching logic. Everything here is pure Python and unit-testable on
any OS so the core can be developed and verified away from a real lab.
"""

from labsentinel_shared.protocol import (
    PROTOCOL_VERSION,
    Envelope,
    decode,
    encode,
)

__all__ = ["PROTOCOL_VERSION", "Envelope", "decode", "encode"]
__version__ = "0.1.0"
