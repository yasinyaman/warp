"""Entrypoint: the `warp` console script and the `warp.main:app` ASGI target.

This is the only HTTP-facing module allowed to touch the composition root.
"""

from warp.adapters.inbound.http.app import create_app
from warp.application.config import RuntimeEnv
from warp.application.container import Container
from warp.infrastructure.bootstrap import build_container
from warp.infrastructure.config_loader import load_config
from warp.infrastructure.logging import setup_logging

ENV = RuntimeEnv.from_environ()


def container_factory() -> Container:
    """Configure logging, load the YAML config and wire the default adapters."""
    setup_logging(level=ENV.log_level, json_format=ENV.log_json)
    return build_container(load_config(ENV.config_path))


app = create_app(container_factory=container_factory, env=ENV)


def main() -> None:
    """Run the API with uvicorn (the `warp` console script)."""
    import uvicorn

    reload = ENV.app_env == "development"
    uvicorn.run(
        "warp.main:app",
        host=ENV.api_host,
        port=ENV.api_port,
        workers=ENV.api_workers if not reload else 1,
        reload=reload,
        access_log=not ENV.is_production,
    )


if __name__ == "__main__":
    main()
