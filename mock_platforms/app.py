from fastapi import FastAPI

from mock_platforms import google, meta, tiktok
from mock_platforms.config import MockPlatformsSettings
from mock_platforms.faults import FaultInjector
from mock_platforms.store import load_store


def create_app(settings: MockPlatformsSettings) -> FastAPI:
    # No docs routes: every route the app serves requires the bearer token.
    app = FastAPI(title="Matchbook mock platforms", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = load_store(settings.delivery_dir)
    app.state.faults = FaultInjector(
        enabled=settings.faults_enabled,
        seed=settings.fault_seed,
        rate_429=settings.rate_429,
        rate_500=settings.rate_500,
    )
    for platform in (meta, google, tiktok):
        app.include_router(platform.router)
    return app
