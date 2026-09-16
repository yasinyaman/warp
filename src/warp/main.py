"""Entrypoint: the `warp` console script and the `warp.main:app` ASGI target."""

from warp.adapters.inbound.http.app import app, create_app, main

__all__ = ["app", "create_app", "main"]

if __name__ == "__main__":
    main()
