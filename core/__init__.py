"""Shared code for Samba_controls and Samba_mini.

Modules directly under ``core`` that start with ``theme``, ``plot_`` or
``data_browser`` and the ``core.gui`` helpers import PyQt6.  Everything else
(protocol, device model, client, scan engine, storage, configuration) is
Qt-free and is imported by the backend service process as well; a test
enforces that.
"""
