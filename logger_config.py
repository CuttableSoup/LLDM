import logging
import sys
def setup_logging(level=logging.INFO):
    """
    Configures the logging system for the application.
    Args:
        level: The logging level (default: logging.INFO).
    """
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)-15s | %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    logging.info("Logging system initialized.")
