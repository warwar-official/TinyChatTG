import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def get_user_logger(user_id: int, logs_path: str = "logs") -> logging.Logger:
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"user_{user_id}")
    if not logger.handlers:
        fh = logging.FileHandler(Path(logs_path) / f"{user_id}.log", encoding='utf-8')
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        # Per-user logs should not propagate to app root logger to avoid duplication
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
    return logger


def get_payload_logger(logs_path: str = "logs/payloads") -> logging.Logger:
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("payload")
    if not logger.handlers:
        log_file = Path(logs_path) / "payloads.log"
        handler = TimedRotatingFileHandler(
            filename=str(log_file),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding='utf-8'
        )
        handler.suffix = "%Y-%m-%d"
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
    return logger


def init_logging(config: dict | None = None):
    cfg = config or {}
    logs_path = (cfg.get('logs_path') if isinstance(cfg, dict) and cfg.get('logs_path') else 'logs')
    debug = bool(cfg.get('debug')) if isinstance(cfg, dict) else True
    Path(logs_path).mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not root.handlers:
        # Main application log: only INFO and above
        fh = logging.FileHandler(Path(logs_path) / 'app.log', encoding='utf-8')
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh.setFormatter(formatter)
        root.addHandler(fh)
        # Root logger set to DEBUG so debug handler can capture detailed logs,
        # but `app.log` will only receive INFO+ due to its handler level.
        root.setLevel(logging.DEBUG)
        # Add a console handler so logs are visible on the terminal
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if debug else logging.INFO)
        ch.setFormatter(formatter)
        root.addHandler(ch)
    else:
        # Ensure there's a console handler present with appropriate level
        has_console = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
        if not has_console:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG if debug else logging.INFO)
            # use same formatter style as file handlers
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            ch.setFormatter(formatter)
            root.addHandler(ch)
