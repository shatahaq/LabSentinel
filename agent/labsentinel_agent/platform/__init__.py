"""Windows-specific implementations of the enforcement ports.

Imports of pywin32/wmi are guarded so the package can be imported on non-Windows
dev machines (the classes raise clearly if actually used off-Windows). Pure
logic lives in enforce/; this module is the thin, hard-to-unit-test edge that is
exercised by the manual test checklist on real hardware.
"""
