"""Warp Catalog CLI - Database Catalog Intelligence.

Commands:
    analyze        - Analyze database and generate catalog
    review         - Interactive table-by-table review of draft catalog
    export         - Export catalog to JSON/YAML/Markdown
    list           - List available catalogs
    info           - Show catalog info
    enrich-openapi - Enrich OpenAPI spec with catalog
    pipeline       - Run full pipeline (DB -> Catalog -> Enriched MCP)
"""

import asyncio
import sys

import click

from warp import __version__


@click.group()
@click.version_option(version=__version__)
@click.option(
    "-c", "--config",
    default="config/database.yaml",
    help="Config file path",
    type=click.Path(),
)
@click.pass_context
def main(ctx, config):
    """Warp Catalog - Database Catalog Intelligence.

    LLM-powered database schema analysis and description generation.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@main.command()
@click.option("-d", "--database", required=True, help="Database config name")
@click.option("-t", "--tables", default=None, help="Comma-separated table names")
@click.option("--lang", default=None, help="Language override")
@click.option("-f", "--format", "fmt", default="json", help="Export format: json, yaml, markdown")
@click.option("-o", "--output", default=None, help="Output file path")
@click.option("--auto-approve", is_flag=True, help="Skip review, approve directly")
@click.pass_context
def analyze(ctx, database, tables, lang, fmt, output, auto_approve):
    """Analyze database and generate catalog."""
    asyncio.run(_run_analyze(ctx, database, tables, lang, fmt, output, auto_approve))


async def _run_analyze(ctx, database, tables, lang, fmt, output, auto_approve=False):
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config
    from warp.core.logging import setup_logging
    from warp.database.factory import DatabaseFactory
    from warp.enrichment.analyzer import EnrichedAnalyzer
    from warp.export.markdown_exporter import get_exporter
    from warp.llm.client import LLMClient

    config = load_config(ctx.obj["config_path"])
    setup_logging(level="INFO")

    lang = lang or config.settings.i18n.default_language
    table_names = tables.split(",") if tables else None

    store = CatalogFileStore(config.settings.catalog.storage_path)

    # Find database config
    db_config = None
    for db in config.databases:
        if db.name == database:
            db_config = db
            break

    if not db_config:
        click.echo(f"Error: Database config '{database}' not found", err=True)
        sys.exit(1)

    adapter_config = {
        "type": db_config.type,
        "host": db_config.host,
        "port": db_config.port,
        "database": db_config.database,
        "username": db_config.username,
        "password": db_config.password,
        **db_config.options,
    }

    adapter = DatabaseFactory.create(adapter_config)
    await adapter.connect()

    try:
        llm_client = LLMClient.from_config(config)
        try:
            analyzer = EnrichedAnalyzer(
                adapter=adapter,
                config=config,
                llm_client=llm_client,
                store=store,
                db_type=db_config.type,
                schema="public" if db_config.type == "postgresql" else db_config.database,
                database_name=database,
            )

            catalog = await analyzer.analyze(
                table_names=table_names,
                auto_approve=auto_approve,
            )
            click.echo(f"Catalog generated: {catalog.table_count} tables, languages: {catalog.languages}")

            if auto_approve:
                click.echo("Status: APPROVED (auto-approved)")
            else:
                click.echo(f"Status: DRAFT - Run 'warp review -d {database}' to review and approve.")

            if output:
                exporter = get_exporter(fmt)
                path = exporter.export(catalog, output, lang=lang)
                click.echo(f"Exported to: {path}")

        finally:
            await llm_client.close()
    finally:
        await adapter.disconnect()


@main.command("export")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option("-f", "--format", "fmt", default="json", help="Export format")
@click.option("-o", "--output", required=True, help="Output file path")
@click.option("--lang", default="en", help="Language for export")
@click.pass_context
def export_catalog(ctx, database, fmt, output, lang):
    """Export an existing catalog."""
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config
    from warp.export.markdown_exporter import get_exporter

    config = load_config(ctx.obj["config_path"])
    store = CatalogFileStore(config.settings.catalog.storage_path)

    catalog = store.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    exporter = get_exporter(fmt)
    path = exporter.export(catalog, output, lang=lang)
    click.echo(f"Exported {catalog.table_count} tables to: {path}")


@main.command("list")
@click.pass_context
def list_catalogs(ctx):
    """List available catalogs."""
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config

    config = load_config(ctx.obj["config_path"])
    store = CatalogFileStore(config.settings.catalog.storage_path)

    catalogs = store.list_catalogs()
    if not catalogs:
        click.echo("No catalogs found.")
        return

    index = store.get_index()
    for name in catalogs:
        entry = index.catalogs.get(name)
        if entry:
            status = getattr(entry, "status", "draft")
            click.echo(
                f"  {name}: {entry.table_count} tables, "
                f"{entry.database_type}, "
                f"langs={entry.languages}, "
                f"v{entry.version}, "
                f"status={status}"
            )
        else:
            click.echo(f"  {name}")


@main.command("info")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option("--lang", default="en", help="Language for descriptions")
@click.pass_context
def show_info(ctx, database, lang):
    """Show catalog info."""
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config

    config = load_config(ctx.obj["config_path"])
    store = CatalogFileStore(config.settings.catalog.storage_path)

    catalog = store.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    click.echo(f"Database: {catalog.database_name}")
    click.echo(f"Type: {catalog.database_type}")
    click.echo(f"Status: {catalog.status.value}")
    click.echo(f"Tables: {catalog.table_count}")
    click.echo(f"Languages: {catalog.languages}")
    click.echo(f"Generated: {catalog.generated_at}")
    click.echo(f"LLM: {catalog.llm_provider}/{catalog.llm_model}")

    desc = catalog.description.get(lang)
    if desc:
        click.echo(f"Description: {desc}")

    click.echo("\nTables:")
    for tname, table in catalog.tables.items():
        human = table.human_name.get(lang) or tname
        tdesc = table.description.get(lang, "")
        rows = f" (~{table.row_count} rows)" if table.row_count else ""
        click.echo(f"  {human} ({tname}): {len(table.columns)} cols{rows}")
        if tdesc:
            click.echo(f"    {tdesc}")


@main.command("review")
@click.option("-d", "--database", required=True, help="Database/catalog name to review")
@click.option("--lang", default="en", help="Language for display and editing")
@click.option("--auto-approve", is_flag=True, help="Approve all without prompting")
@click.pass_context
def review_catalog(ctx, database, lang, auto_approve):
    """Interactive table-by-table review of a draft catalog.

    Review LLM-generated descriptions and edit any field before approving.
    """
    from warp.catalog.models import CatalogStatus, TableReviewStatus
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config

    config = load_config(ctx.obj["config_path"])
    store = CatalogFileStore(config.settings.catalog.storage_path)

    catalog = store.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    if catalog.status == CatalogStatus.approved:
        click.echo(f"Catalog '{database}' is already approved.")
        if not click.confirm("Re-open for review?"):
            return
        catalog.status = CatalogStatus.draft
        for table in catalog.tables.values():
            table.review_status = TableReviewStatus.pending
        store.save(catalog)

    click.echo(f"\n{'='*60}")
    click.echo(f" Review: {catalog.database_name} ({catalog.table_count} tables)")
    click.echo(f" Status: {catalog.status.value}")
    click.echo(f" Languages: {catalog.languages}")
    click.echo(f"{'='*60}\n")

    if auto_approve:
        store.approve_catalog(database)
        click.echo("All tables auto-approved. Catalog status: approved.")
        return

    table_names = list(catalog.tables.keys())
    for i, tname in enumerate(table_names, 1):
        table = catalog.tables[tname]

        override_marker = " [user-edited]" if table.user_overrides else ""
        click.echo(f"\n--- Table {i}/{len(table_names)}: {tname}{override_marker} ---")
        click.echo(f"  Human name:  {table.human_name.get(lang) or '(empty)'}")
        click.echo(f"  Description: {table.description.get(lang) or '(empty)'}")
        click.echo(f"  Tags:        {', '.join(table.tags) if table.tags else '(none)'}")
        click.echo(f"  Rows:        {table.row_count or 'N/A'}")
        click.echo(f"  Columns ({len(table.columns)}):")

        for col in table.columns:
            pk_marker = " [PK]" if col.is_primary_key else ""
            fk_marker = f" [FK->{col.references}]" if col.is_foreign_key else ""
            sem = f" ({col.semantic_type})" if col.semantic_type else ""
            desc = col.description.get(lang) or ""
            click.echo(f"    {col.name} ({col.data_type}){pk_marker}{fk_marker}{sem}")
            if desc:
                click.echo(f"      {desc}")
            if col.tags:
                click.echo(f"      tags: {', '.join(col.tags)}")

        if table.relationships:
            click.echo(f"  Relationships ({len(table.relationships)}):")
            for rel in table.relationships:
                click.echo(
                    f"    {rel.source_column} -> {rel.target_table}.{rel.target_column}"
                    f" ({rel.relationship_type})"
                )
                if not rel.description.is_empty:
                    click.echo(f"      {rel.description.get(lang)}")

        click.echo()

        while True:
            action = click.prompt(
                "  Action: [a]pprove, [e]dit, [s]kip, [q]uit",
                type=click.Choice(["a", "e", "s", "q"], case_sensitive=False),
                default="a",
            )

            if action == "a":
                store.approve_table(database, tname)
                click.echo(f"  -> {tname} approved.")
                break
            elif action == "s":
                click.echo(f"  -> {tname} skipped (remains pending).")
                break
            elif action == "q":
                click.echo("\nReview paused. Run 'warp review' to continue.")
                return
            elif action == "e":
                _interactive_edit_table(store, database, tname, table, lang)
                # Reload table after edits
                catalog = store.load_or_raise(database)
                table = catalog.tables[tname]
                click.echo(f"\n  Updated {tname}:")
                click.echo(f"    Human name:  {table.human_name.get(lang)}")
                click.echo(f"    Description: {table.description.get(lang)}")
                click.echo(f"    Tags:        {', '.join(table.tags) if table.tags else '(none)'}")
                continue  # Re-prompt action

    # Final check
    catalog = store.load_or_raise(database)
    summary = catalog.review_summary
    click.echo(f"\nReview summary: {summary}")

    pending_count = summary.get("pending", 0)
    if pending_count > 0:
        if click.confirm(f"{pending_count} table(s) still pending. Approve all remaining?"):
            store.approve_catalog(database)
            click.echo("Catalog approved.")
        else:
            click.echo("Catalog remains in draft status.")
    else:
        store.approve_catalog(database)
        click.echo("All tables reviewed. Catalog approved!")


def _interactive_edit_table(store, db_name, table_name, table, lang):
    """Interactive edit sub-menu for a single table."""
    while True:
        click.echo("\n  Edit fields:")
        click.echo("    1. Description")
        click.echo("    2. Human name")
        click.echo("    3. Tags")
        click.echo("    4. Column description")
        click.echo("    5. Column semantic_type")
        click.echo("    6. Column tags")
        click.echo("    7. Done editing")

        choice = click.prompt("  Choose", type=int, default=7)

        if choice == 1:
            new_val = click.prompt(
                f"  New description ({lang})",
                default=table.description.get(lang),
            )
            store.update_table_fields(db_name, table_name, {"description": new_val}, lang)
            click.echo("  -> Description updated.")

        elif choice == 2:
            new_val = click.prompt(
                f"  New human name ({lang})",
                default=table.human_name.get(lang),
            )
            store.update_table_fields(db_name, table_name, {"human_name": new_val}, lang)
            click.echo("  -> Human name updated.")

        elif choice == 3:
            current = ", ".join(table.tags) if table.tags else ""
            new_val = click.prompt(
                "  Tags (comma-separated)",
                default=current,
            )
            tags = [t.strip() for t in new_val.split(",") if t.strip()]
            store.update_table_fields(db_name, table_name, {"tags": tags}, lang)
            click.echo("  -> Tags updated.")

        elif choice in (4, 5, 6):
            col_names = [c.name for c in table.columns]
            click.echo(f"  Columns: {', '.join(col_names)}")
            col_name = click.prompt("  Column name")
            col = table.get_column(col_name)
            if not col:
                click.echo(f"  Column '{col_name}' not found.")
                continue

            if choice == 4:
                new_val = click.prompt(
                    f"  New description ({lang})",
                    default=col.description.get(lang),
                )
                store.update_column_fields(
                    db_name, table_name, col_name,
                    {"description": new_val}, lang,
                )
                click.echo("  -> Column description updated.")

            elif choice == 5:
                new_val = click.prompt(
                    "  New semantic_type",
                    default=col.semantic_type or "",
                )
                store.update_column_fields(
                    db_name, table_name, col_name,
                    {"semantic_type": new_val if new_val else None}, lang,
                )
                click.echo("  -> Semantic type updated.")

            elif choice == 6:
                current = ", ".join(col.tags) if col.tags else ""
                new_val = click.prompt(
                    "  Tags (comma-separated)",
                    default=current,
                )
                tags = [t.strip() for t in new_val.split(",") if t.strip()]
                store.update_column_fields(
                    db_name, table_name, col_name,
                    {"tags": tags}, lang,
                )
                click.echo("  -> Column tags updated.")

        elif choice == 7:
            break
        else:
            click.echo("  Invalid choice.")

        # Reload to reflect changes
        catalog = store.load_or_raise(db_name)
        table = catalog.tables[table_name]


@main.command("enrich-openapi")
@click.option("-d", "--database", required=True, help="Catalog name")
@click.option("-i", "--input", "input_path", required=True, help="OpenAPI spec file")
@click.option("-o", "--output", default=None, help="Output file path")
@click.option("--lang", default="en", help="Language for descriptions")
@click.pass_context
def enrich_openapi(ctx, database, input_path, output, lang):
    """Enrich OpenAPI spec with catalog descriptions."""
    from warp.catalog.store import CatalogFileStore
    from warp.config.settings import load_config
    from warp.integration.openapi_enricher import OpenAPIEnricher

    config = load_config(ctx.obj["config_path"])
    store = CatalogFileStore(config.settings.catalog.storage_path)

    catalog = store.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    enricher = OpenAPIEnricher(catalog, lang=lang)
    result_path = enricher.enrich_file(input_path, output)
    click.echo(f"Enriched spec saved to: {result_path}")


@main.command("pipeline")
@click.option("-d", "--database", required=True, help="Database config name")
@click.option("--lang", default=None, help="Language")
@click.option("-t", "--tables", default=None, help="Comma-separated table names")
@click.option("-f", "--format", "fmt", default=None, help="Export format")
@click.option("-o", "--output", default=None, help="Export output path")
@click.option("--openapi", default=None, help="OpenAPI spec to enrich")
@click.pass_context
def run_pipeline(ctx, database, lang, tables, fmt, output, openapi):
    """Run full pipeline: DB -> Catalog -> Enriched MCP."""
    asyncio.run(_run_pipeline(ctx, database, lang, tables, fmt, output, openapi))


async def _run_pipeline(ctx, database, lang, tables, fmt, output, openapi):
    from warp.config.settings import load_config
    from warp.core.logging import setup_logging
    from warp.integration.pipeline import Pipeline

    config = load_config(ctx.obj["config_path"])
    setup_logging(level="INFO")

    table_names = tables.split(",") if tables else None

    pipeline = Pipeline(config)
    result = await pipeline.run(
        database_name=database,
        lang=lang,
        table_names=table_names,
        export_format=fmt,
        export_path=output,
        openapi_spec_path=openapi,
    )

    click.echo(f"Pipeline completed: {result.catalog.table_count} tables analyzed")
    if result.export_path:
        click.echo(f"Exported to: {result.export_path}")
    if result.enriched_openapi_path:
        click.echo(f"Enriched OpenAPI: {result.enriched_openapi_path}")


if __name__ == "__main__":
    main()
