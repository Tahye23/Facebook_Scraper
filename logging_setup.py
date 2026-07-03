import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in ("platform", "service", "scrape_id", "url", "post_id"):
            value = getattr(record, key, None)
            if value not in (None, ""):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class _DefaultContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "platform"):
            record.platform = "unknown"
        if not hasattr(record, "service"):
            record.service = "unknown"
        if not hasattr(record, "scrape_id"):
            record.scrape_id = None
        if not hasattr(record, "url"):
            record.url = None
        if not hasattr(record, "post_id"):
            record.post_id = None
        return True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def setup_logging() -> None:
    if getattr(setup_logging, "_configured", False):
        return

    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = (os.getenv("LOG_FORMAT") or "json").strip().lower()
    log_to_console = _env_bool("LOG_TO_CONSOLE", True)

    repo_root = Path(__file__).resolve().parent
    log_file = (os.getenv("LOG_FILE") or "").strip()
    if not log_file:
        log_dir = (os.getenv("LOG_DIR") or "logs").strip()
        log_path = repo_root / log_dir / "scraper.log"
    else:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = repo_root / log_path

    rotation_mb = int((os.getenv("LOG_ROTATION_MB") or "10").strip())
    backup_count = int((os.getenv("LOG_BACKUP_COUNT") or "5").strip())

    formatter: logging.Formatter
    if log_format == "text":
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(platform)s/%(service)s] %(message)s"
        )
    else:
        formatter = JsonFormatter()

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.filters.clear()
    root_logger.addFilter(_DefaultContextFilter())

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=max(1, rotation_mb) * 1024 * 1024,
        backupCount=max(1, backup_count),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    setup_logging._configured = True


class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = dict(self.extra)
        if "extra" in kwargs and isinstance(kwargs["extra"], dict):
            extra.update(kwargs["extra"])
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str, platform: str | None = None, service: str | None = None) -> ContextAdapter:
    setup_logging()
    extra = {}
    if platform:
        extra["platform"] = platform
    if service:
        extra["service"] = service
    return ContextAdapter(logging.getLogger(name), extra)


def with_context(logger: ContextAdapter, **kwargs) -> ContextAdapter:
    merged = dict(logger.extra)
    merged.update({k: v for k, v in kwargs.items() if v is not None})
    return ContextAdapter(logger.logger, merged)
