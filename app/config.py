import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")


def _load_yaml() -> dict:
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


class AppConfig:
    def __init__(self):
        self._raw = _load_yaml()
        self.google_api_key: str = os.environ.get("GOOGLE_API_KEY", "")
        self.base_dir: Path = BASE_DIR

        # ── Calendar / Scheduling ──────────────────────────────────────────────
        self.cal_api_key: str = os.environ.get("CAL_API_KEY", "")
        self.cal_event_type_id: str = os.environ.get("CAL_EVENT_TYPE_ID", "")
        self.cal_api_base_url: str = os.environ.get("CAL_API_BASE_URL", "https://api.cal.com/v2")
        self.default_timezone: str = os.environ.get("DEFAULT_TIMEZONE", "Asia/Kolkata")

    @property
    def server_host(self) -> str:
        return self._raw.get("server", {}).get("host", "0.0.0.0")

    @property
    def server_port(self) -> int:
        return int(self._raw.get("server", {}).get("port", 8000))

    @property
    def gemini_model(self) -> str:
        return self._raw.get("gemini", {}).get("model", "gemini-2.0-flash-exp")

    @property
    def gemini_voice_name(self) -> str:
        return self._raw.get("gemini", {}).get("voice_name", "Aoede")

    @property
    def system_instruction(self) -> str:
        return self._raw.get("gemini", {}).get("system_instruction", "")

    @property
    def audio_config(self) -> dict:
        return self._raw.get("gemini", {}).get("audio", {})

cfg = AppConfig()
