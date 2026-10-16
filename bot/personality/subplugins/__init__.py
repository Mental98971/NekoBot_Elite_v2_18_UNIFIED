"""Internal intent-routing plugins for the personality layer.

v2.1: Added MetaBanterPlugin for conversational dynamics (tags, emotions,
introvert reveals, off-topic call-outs, etc.) observed in real chat logs.
"""
from .base import Plugin, PluginManager, PluginResult
from .programming import ProgrammingPlugin
from .anime import AnimePlugin
from .gaming import GamingPlugin
from .general import GeneralPlugin
from .meta_banter import MetaBanterPlugin

__all__ = [
    "Plugin", "PluginManager", "PluginResult",
    "ProgrammingPlugin", "AnimePlugin", "GamingPlugin", "GeneralPlugin",
    "MetaBanterPlugin",
]
