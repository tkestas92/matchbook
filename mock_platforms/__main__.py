import uvicorn

from mock_platforms.app import create_app
from mock_platforms.config import MockPlatformsSettings


def main() -> None:
    settings = MockPlatformsSettings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    main()
