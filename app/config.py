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
        self.api_key: str = os.environ.get("DEEPGRAM_API_KEY", "")
        self.base_dir: Path = BASE_DIR

    @property
    def deepgram_url(self) -> str:
        return self._raw["deepgram"]["url"]

    @property
    def audio_config(self) -> dict:
        return self._raw["deepgram"]["audio"]

    @property
    def http_port(self) -> int:
        return self._raw["server"]["http_port"]

    @property
    def ws_port(self) -> int:
        return self._raw["server"]["ws_port"]

    @property
    def defaults(self) -> dict:
        return self._raw["agent"]["defaults"]

    @property
    def models(self) -> dict:
        return self._raw["agent"]["models"]


cfg = AppConfig()
