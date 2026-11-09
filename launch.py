"""
Amethyst launch hook: keep DIP output mods up to date before the game starts.

Amethyst calls before_launch(context) after deploying and before launching.
The work is in dip_core.autopatch; see its docstring.
"""

try:  # Amethyst loads this file as a package rooted at the plugin folder
    from .dip_core import autopatch
except ImportError:  # loaded on its own (tests, tools)
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dip_core import autopatch  # type: ignore[no-redef]


def before_launch(context):
    return autopatch.before_launch(context)
