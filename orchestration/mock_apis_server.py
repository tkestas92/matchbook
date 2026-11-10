"""Serve the mock platform APIs to other containers (`make mock-apis` binds 127.0.0.1 only)."""

import uvicorn

from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings

ALL_INTERFACES = "0.0.0.0"  # Inside the airflow compose profile only; no host port is published.


def main() -> None:
    settings = MockPlatformsSettings()
    uvicorn.run(create_app(settings), host=ALL_INTERFACES, port=settings.port)


if __name__ == "__main__":
    main()
