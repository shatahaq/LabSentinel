"""Agent enforcement layer.

Pure decision logic (safety gate, whitelist matching, enforcement flow) kept
separate from Windows-specific I/O (process enumeration, window queries, closing
processes), which lives behind interfaces so the logic is fully unit-testable
with fakes. The safety gate is the most important code in the system: it must
never allow a system/protected process to be acted on.
"""
