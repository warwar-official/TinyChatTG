import logging
from pathlib import Path


def get_user_logger(user_id: int, logs_path: str = "logs") -> logging.Logger:
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"user_{user_id}")
    if not logger.handlers:
        fh = logging.FileHandler(Path(logs_path) / f"{user_id}.log", encoding='utf-8')
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        logger.setLevel(logging.DEBUG)
    return logger


def init_logging(config: dict | None = None):
    cfg = config or {}
    logs_path = (cfg.get('logs_path') if isinstance(cfg, dict) and cfg.get('logs_path') else 'logs')
    debug = bool(cfg.get('debug')) if isinstance(cfg, dict) else True
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not root.handlers:
        fh = logging.FileHandler(Path(logs_path) / 'app.log', encoding='utf-8')
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh.setFormatter(formatter)
        root.addHandler(fh)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
