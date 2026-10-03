"""Browser policy management (site blocking) via Windows registry policies.

Registry access sits behind the ``RegistryPort`` interface so the policy logic —
including the parts that must never damage an institution's own policies — is
fully unit-testable with an in-memory fake, on any OS.
"""
