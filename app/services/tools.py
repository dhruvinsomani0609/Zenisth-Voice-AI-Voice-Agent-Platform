"""
Deprecated shim.

The Gemini Live server now uses the real tool registry in `agent/functions.py`.
This module is kept only to avoid breaking older imports during migration.
"""

from __future__ import annotations

from typing import Any, Dict

from agent.functions import dispatch, get_schemas


AVAILABLE_TOOLS = get_schemas()


async def execute_tool(func_name: str, kwargs: Dict[str, Any]) -> Any:
    return await dispatch(func_name, kwargs)
