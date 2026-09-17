# API Reference

Selected internal modules, generated from docstrings.

## SQL identifiers

The single source of truth for validating/quoting dynamic SQL identifiers.

::: warp.adapters.outbound.db.identifiers

## Named parameters for raw SQL

::: warp.adapters.outbound.db.params.bind_named_params

## Ports

::: warp.application.ports.database

::: warp.application.ports.catalog_repository

::: warp.application.ports.text_generation

## Composition root

::: warp.application.container.Container

::: warp.infrastructure.bootstrap.build_container

## Configuration

::: warp.application.config.validate_production_config

## Data access for external engines

Typed schema, planner row estimates, streaming export and the `/info`
capabilities block let an OLAP engine (e.g. Fusion) pull exactly the rows it
needs instead of paging through a whole table.

::: warp.adapters.inbound.http.capabilities.capabilities_of

::: warp.adapters.inbound.http.routes.schema

::: warp.adapters.inbound.http.routes.export

::: warp.adapters.inbound.http.arrow_export

::: warp.application.config.ExportConfig
