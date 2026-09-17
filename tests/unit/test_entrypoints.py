"""The thin entry points expose the ASGI app and the CLI with the composition root injected."""

import warp.cli
import warp.main
from warp.adapters.inbound.http.app import runtime_of
from warp.application.config import RuntimeEnv


def test_asgi_target():
    assert warp.main.app.title == "Warp Engine"
    assert isinstance(runtime_of(warp.main.app).env, RuntimeEnv)


def test_cli_has_container_factory():
    assert warp.cli.main.context_settings["obj"]["container_factory"] is warp.cli.container_factory
