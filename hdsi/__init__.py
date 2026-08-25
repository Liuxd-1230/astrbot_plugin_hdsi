"""HDS Interlude core package (AstrBot port).

The modules under this package must stay importable without AstrBot installed
so the whole domain layer is unit-testable. AstrBot-specific bindings live in
the plugin root (main.py).
"""

__version__ = "1.0.0"
PLUGIN_NAME = "astrbot_plugin_hdsi"
