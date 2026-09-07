"""Public FastAPI application surface for governed analytics."""

from governed_analytics.api.app import app, create_app
from governed_analytics.api.dependencies import AppContainer, build_container

__all__ = ["AppContainer", "app", "build_container", "create_app"]
