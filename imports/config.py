import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "data" / "configs" / "app_config.yaml"


def load_config(path: Path = None):
    p = path or CONFIG_PATH
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


CONFIG = load_config()


def get_provider(name: str):
    return (CONFIG.get("providers") or {}).get(name)


def get_main_model():
    return CONFIG.get("models", {}).get("main_model")


def get_mcp(name: str):
    return (CONFIG.get("mcp") or {}).get(name)


def get_memory_config():
    return CONFIG.get("memory", {})


def get_bot_config():
    return CONFIG.get("bot", {})


def get_logging_config():
    return CONFIG.get("logging", {})


def get_telegram_token():
    return os.environ.get("TELEGRAM_TOKEN")
