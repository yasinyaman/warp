"""REST API layer: routers, authentication, and CRUD endpoints."""

from .crud import CRUDOperations
from .router_factory import RouterFactory

__all__ = ["RouterFactory", "CRUDOperations"]
