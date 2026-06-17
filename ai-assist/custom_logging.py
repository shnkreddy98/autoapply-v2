import logging
import os
from datetime import datetime

log_path = "logs"


def get_logger():
    os.makedirs(log_path, exist_ok=True)
    now = datetime.now()
    FORMAT = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG,
        format=FORMAT,
        handlers=[
            logging.FileHandler(
                os.path.join(log_path, f"{now.strftime('%Y%m%d%H%M%S')}.log")
            ),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
