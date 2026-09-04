"""FastAPI application factory and default ASGI application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from governed_analytics.api.dependencies import AppContainer, build_container
from governed_analytics.api.routes import analysis_router, health_router

type ContainerFactory = Callable[[], AbstractAsyncContextManager[AppContainer]]


def create_app(*, container_factory: ContainerFactory = build_container) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with container_factory() as container:
            application.state.container = container
            try:
                yield
            finally:
                del application.state.container

    application = FastAPI(title="Governed Analytics Agent", lifespan=lifespan)
    application.include_router(analysis_router)
    application.include_router(health_router)
    return application


app = create_app()

__all__ = ["ContainerFactory", "app", "create_app"]
