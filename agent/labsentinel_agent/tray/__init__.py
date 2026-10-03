"""Participant-visible status indicator (system tray) and warning popups.

The agent runs as a Windows Service in session 0, which cannot draw on the
logged-in user's desktop. A small helper process therefore runs in the user's
session and renders the tray icon and warning popups, driven by the service over
a loopback IPC channel.

Transparency is the point of this package: the participant must always be able
to see that monitoring is active. The channel is display-only — the service
never accepts control instructions from the helper, so a participant cannot
pause monitoring by talking to it.
"""
