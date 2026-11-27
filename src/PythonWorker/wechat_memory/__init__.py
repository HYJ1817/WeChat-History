"""Native worker services shared by the WinUI app.

This package deliberately has no import-time side effects: no process scan, no
network, no database access. Each module is wired by worker.py.
"""
