"""GrokX CLI package."""
__version__ = "1.5.0"


def main():
    # Imported lazily: `grokx_cli.mcp_server` and `grokx_cli.server` must not pull in the CLI
    # (rich console, prompt_toolkit) just because the package is imported.
    from grokx_cli.cli import main_sync

    main_sync()
