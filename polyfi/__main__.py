import logging

import uvicorn

from .config import Settings
from .server import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if settings.host not in ("127.0.0.1", "localhost") and not settings.dashboard_token:
        raise SystemExit("Refusing to expose the dashboard without POLYFI_DASHBOARD_TOKEN")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
