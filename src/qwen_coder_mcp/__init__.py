"""qwen_coder_mcp — MCP server + Qwen3.6-27B client."""
from .config import Settings, load_settings
from .qwen_client import QwenClient

__all__ = ["Settings", "load_settings", "QwenClient"]
__version__ = "0.1.0"
