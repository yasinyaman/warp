"""Entrypoint: the `warp-catalog` console script."""

from warp.adapters.inbound.cli.main import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
