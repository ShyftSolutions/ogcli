#!/usr/bin/env python3
"""
Interactive TUI for WMS CLI with fuzzy search and autocomplete.
"""

from textual.app import App, ComposeResult
from textual.containers import Container, Vertical, Horizontal, ScrollableContainer
from textual.widgets import Header, Footer, Input, Static, Label, RichLog
from textual.suggester import Suggester
from textual.binding import Binding
from textual import on, events
from rich.text import Text
from rich.table import Table
from rich.panel import Panel
from pathlib import Path
import sys
from typing import List, Optional

from .config import load_config, get_capabilities_xml, WMSConfig
from .parser import parse_capabilities, Layer
from .fuzzy import FuzzySearchEngine, SearchResult
from .resolver import SmartQueryResolver, ResolvedQuery, AmbiguousQueryError
from .client import WMSClient


class WMSSuggester(Suggester):
    """Suggester for WMS commands and layer names."""

    def __init__(self, tui_app):
        super().__init__()
        self.tui_app = tui_app

    async def get_suggestion(self, value: str) -> str | None:
        """Get suggestion based on current input - shows gray completion text."""
        return self.tui_app.get_first_suggestion(value)


class WMSTui(App):
    """Interactive TUI for WMS CLI"""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 100%;
        layout: vertical;
    }

    #output-log {
        height: 1fr;
        background: $surface;
        padding: 1 2;
    }

    #suggestions-container {
        height: auto;
        max-height: 10;
        background: $surface-darken-1;
        border-top: solid $primary;
        padding: 0 2;
    }

    #input-container {
        height: auto;
        background: $surface-darken-1;
        border-top: solid $accent;
        padding: 1 2;
    }

    #prompt-label {
        color: $accent;
        width: auto;
    }

    #query-input {
        width: 1fr;
        border: none;
        background: transparent;
        padding: 0 1;
    }

    #query-input:focus {
        border: none;
    }

    .suggestion-line {
        color: $text-muted;
    }

    .suggestion-selected {
        background: $accent;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+d", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("escape", "cancel", "Clear"),
    ]

    def __init__(self):
        super().__init__()
        self.config: Optional[WMSConfig] = None
        self.engine: Optional[FuzzySearchEngine] = None
        self.resolver: Optional[SmartQueryResolver] = None
        self.current_results: List[SearchResult] = []
        self.selected_index: int = 0
        self.current_query_prefix: str = ""  # Store the command part (e.g., "map ")

        # Available commands for autocomplete
        self.commands = ["init", "map", "tile", "legend", "layers", "status", "help"]

    def compose(self) -> ComposeResult:
        """Build the UI layout"""
        yield Header()

        with Container(id="main-container"):
            # Output log at the top (takes most space)
            yield RichLog(id="output-log", wrap=True, highlight=True, markup=True)

            # Suggestions panel (hidden when empty)
            yield ScrollableContainer(Static("", id="suggestions"), id="suggestions-container")

            # Command input at the bottom
            with Horizontal(id="input-container"):
                yield Label("wms>", id="prompt-label")
                yield Input(
                    placeholder="Type command (e.g., 'map gfs temp', 'init <source>', 'help')...",
                    id="query-input",
                    suggester=WMSSuggester(self)
                )

        yield Footer()

    def on_mount(self) -> None:
        """Called when app starts"""
        self.title = "WMS CLI - Interactive Mode"

        # Load config and initialize search engine
        output_log = self.query_one("#output-log", RichLog)
        try:
            self.config = load_config()

            # Parse capabilities
            caps_xml = get_capabilities_xml(self.config)
            capabilities = parse_capabilities(caps_xml)

            # Initialize search engine and resolver
            self.engine = FuzzySearchEngine(capabilities)
            self.resolver = SmartQueryResolver(capabilities)

            # Welcome message
            output_log.write("[bold cyan]WMS CLI - Interactive Shell[/bold cyan]")
            output_log.write("")
            output_log.write("[bold green]✓ Configuration loaded[/bold green]")
            output_log.write(f"  Server: {self.config.server_url}")
            output_log.write(f"  Layers: {len(self.engine.get_all_layers())}")
            output_log.write("")
            output_log.write("[bold]Available Commands:[/bold]")
            output_log.write("  [cyan]init <source>[/cyan]     - Initialize with capabilities file or server URL")
            output_log.write("  [cyan]map <query>[/cyan]       - Fetch a map image")
            output_log.write("  [cyan]tile <query>[/cyan]      - Fetch a random tile")
            output_log.write("  [cyan]legend <query>[/cyan]    - Fetch a legend graphic")
            output_log.write("  [cyan]layers [query][/cyan]    - List layers (optionally filtered)")
            output_log.write("  [cyan]status[/cyan]            - Show configuration")
            output_log.write("  [cyan]help[/cyan]              - Show this help")
            output_log.write("")
            output_log.write("[dim]Type a command and press Enter to execute[/dim]")
            output_log.write("")

        except Exception as e:
            output_log.write("[bold cyan]WMS CLI - Interactive Shell[/bold cyan]")
            output_log.write("")
            output_log.write(f"[bold red]✗ No configuration found[/bold red]")
            output_log.write("")
            output_log.write("To get started, run:")
            output_log.write("  [cyan]init ./capabilities.xml[/cyan]")
            output_log.write("  [cyan]init http://your-server/wms[/cyan]")
            output_log.write("")
            output_log.write("Then you can use commands like:")
            output_log.write("  [cyan]map gfs temp[/cyan]")
            output_log.write("  [cyan]tile hrrr precip[/cyan]")
            output_log.write("  [cyan]layers cloud[/cyan]")
            output_log.write("")

        # Focus on input
        self.query_one("#query-input", Input).focus()

    def on_key(self, event: events.Key) -> None:
        """Handle key presses for autocomplete"""
        query_input = self.query_one("#query-input", Input)

        # Only handle if input is focused
        if not query_input.has_focus:
            return

        if event.key == "right":
            # Accept inline suggestion
            self.accept_suggestion()
            event.prevent_default()
            event.stop()
        elif event.key == "up":
            # Previous suggestion
            if self.current_results:
                self.selected_index = (self.selected_index - 1) % len(self.current_results)
                self.update_suggestions(self.current_results)
                event.prevent_default()
                event.stop()
        elif event.key == "down":
            # Next suggestion
            if self.current_results:
                self.selected_index = (self.selected_index + 1) % len(self.current_results)
                self.update_suggestions(self.current_results)
                event.prevent_default()
                event.stop()

    @on(Input.Changed, "#query-input")
    def on_query_changed(self, event: Input.Changed) -> None:
        """Handle query input changes - update suggestions"""
        query = event.value.strip()

        if not query:
            self.update_suggestions([])
            self.current_query_prefix = ""
            return

        # Parse command and query
        parts = query.split(maxsplit=1)
        command = parts[0].lower()
        search_query = parts[1] if len(parts) > 1 else ""

        # If typing just the command (no space yet), show command completions
        if len(parts) == 1:
            matching_commands = [cmd for cmd in self.commands if cmd.startswith(command)]
            if len(matching_commands) > 0 and command != matching_commands[0]:
                # Show command suggestions
                self.current_query_prefix = ""
                self.update_command_suggestions(matching_commands)
                return
            else:
                self.update_suggestions([])
                self.current_query_prefix = ""
                return

        # We have a command and a query
        self.current_query_prefix = f"{command} "

        # Only show layer suggestions for commands that need them
        if not self.engine:
            self.update_suggestions([])
            return

        if command in ["map", "tile", "legend", "layers"] and search_query:
            # Extract dimension tokens before searching (if resolver available)
            if self.resolver:
                try:
                    extracted_dims, extracted_units, clean_query = self.resolver._extract_tokens(search_query)
                except (AttributeError, TypeError, ValueError):
                    clean_query = search_query
            else:
                clean_query = search_query

            results = self.engine.search(clean_query, limit=10)

            # Stop showing suggestions if best match is >= 80% confidence
            if results and results[0].score >= 80.0:
                self.current_results = []
                self.selected_index = 0
                self.update_suggestions([])
            else:
                self.current_results = results
                self.selected_index = 0
                self.update_suggestions(results)
        else:
            self.update_suggestions([])

    def update_suggestions(self, results: List[SearchResult]) -> None:
        """Update the suggestions display"""
        suggestions_widget = self.query_one("#suggestions", Static)
        suggestions_container = self.query_one("#suggestions-container")

        if not results:
            suggestions_widget.update("")
            suggestions_container.display = False
            return

        suggestions_container.display = True

        # Build suggestions text
        text = Text()
        for i, result in enumerate(results):
            prefix = "▶ " if i == self.selected_index else "  "
            style = "bold cyan" if i == self.selected_index else "white"

            # Format: layer name + score
            layer_name = result.layer.title or result.layer.name
            score_text = f"({result.score:.0f})"

            text.append(f"{prefix}{layer_name} ", style=style)
            text.append(score_text, style="dim")
            text.append("\n")

        suggestions_widget.update(text)

    def update_command_suggestions(self, commands: List[str]) -> None:
        """Update suggestions to show command completions"""
        suggestions_widget = self.query_one("#suggestions", Static)
        suggestions_container = self.query_one("#suggestions-container")

        if not commands:
            suggestions_widget.update("")
            suggestions_container.display = False
            return

        suggestions_container.display = True

        # Build suggestions text
        text = Text()
        for i, cmd in enumerate(commands):
            prefix = "▶ " if i == 0 else "  "
            style = "bold cyan" if i == 0 else "white"

            text.append(f"{prefix}{cmd}", style=style)
            text.append("\n")

        suggestions_widget.update(text)

    @on(Input.Submitted, "#query-input")
    def on_query_submitted(self, event: Input.Submitted) -> None:
        """Handle query submission"""
        query = event.value.strip()

        if not query:
            return

        # Clear input
        event.input.value = ""
        self.current_results = []
        self.selected_index = 0
        self.update_suggestions([])

        # Execute command
        self.execute_command(query)

    def execute_command(self, query: str) -> None:
        """Execute a command"""
        output_log = self.query_one("#output-log", RichLog)
        output_log.write(f"[cyan]wms>[/cyan] {query}")

        parts = query.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        try:
            if command == "help":
                self.show_help()
            elif command == "init":
                self.init_config(args)
            elif command == "status":
                self.show_status()
            elif command == "layers":
                self.list_layers(args)
            elif command == "map":
                if not self.config or not self.engine or not self.resolver:
                    output_log.write("[bold red]✗ No configuration loaded. Run: init <source>[/bold red]")
                    return
                self.fetch_map(args)
            elif command == "tile":
                if not self.config or not self.engine or not self.resolver:
                    output_log.write("[bold red]✗ No configuration loaded. Run: init <source>[/bold red]")
                    return
                self.fetch_tile(args)
            elif command == "legend":
                if not self.config or not self.engine or not self.resolver:
                    output_log.write("[bold red]✗ No configuration loaded. Run: init <source>[/bold red]")
                    return
                self.fetch_legend(args)
            else:
                output_log.write(f"[bold red]✗ Unknown command: {command}[/bold red]")
                output_log.write("Type 'help' for available commands")
        except Exception as e:
            output_log.write(f"[bold red]✗ Error: {e}[/bold red]")

        output_log.write("")

    def show_help(self) -> None:
        """Show help message"""
        output_log = self.query_one("#output-log", RichLog)
        output_log.write("[bold]Available Commands:[/bold]")
        output_log.write("  [cyan]init <source>[/cyan]     - Initialize with capabilities file or server URL")
        output_log.write("  [cyan]map <query>[/cyan]       - Fetch a map image")
        output_log.write("  [cyan]tile <query>[/cyan]      - Fetch a random tile")
        output_log.write("  [cyan]legend <query>[/cyan]    - Fetch a legend graphic")
        output_log.write("  [cyan]layers [query][/cyan]    - List layers (optionally filtered)")
        output_log.write("  [cyan]status[/cyan]            - Show configuration")
        output_log.write("  [cyan]help[/cyan]              - Show this help")
        output_log.write("")
        output_log.write("[bold]Examples:[/bold]")
        output_log.write("  [dim]init https://ogc.shyftwx.com/ogc/WMS[/dim]")
        output_log.write("  [dim]map gfs temp[/dim]")
        output_log.write("  [dim]tile hrrr precip +6h[/dim]")
        output_log.write("  [dim]legend galwem cloud 850[/dim]")
        output_log.write("  [dim]layers cloud[/dim]")

    def init_config(self, source: str) -> None:
        """Initialize configuration"""
        output_log = self.query_one("#output-log", RichLog)

        if not source:
            output_log.write("[bold red]✗ Please provide a source[/bold red]")
            output_log.write("Usage:")
            output_log.write("  init ./capabilities.xml")
            output_log.write("  init http://your-server/wms")
            return

        output_log.write(f"Initializing from: {source}")

        try:
            from .config import init_from_file, init_from_server

            # Detect if it's a URL or file path
            if source.startswith('http://') or source.startswith('https://'):
                output_log.write("Fetching from server...")
                config = init_from_server(source)
            else:
                output_log.write("Reading from file...")
                config = init_from_file(source)

            output_log.write("[bold green]✓ Configuration saved[/bold green]")

            # Reload configuration
            self.config = config
            caps_xml = get_capabilities_xml(config)
            capabilities = parse_capabilities(caps_xml)
            self.engine = FuzzySearchEngine(capabilities)
            self.resolver = SmartQueryResolver(capabilities)

            output_log.write(f"  Server: {config.server_url}")
            output_log.write(f"  Layers: {len(self.engine.get_all_layers())}")
            output_log.write("")
            output_log.write("Ready! Try: [cyan]map gfs temp[/cyan]")

        except Exception as e:
            output_log.write(f"[bold red]✗ Error: {e}[/bold red]")

    def show_status(self) -> None:
        """Show configuration status"""
        output_log = self.query_one("#output-log", RichLog)

        if not self.config or not self.engine:
            output_log.write("[bold red]✗ No configuration loaded[/bold red]")
            output_log.write("Run: [cyan]init <source>[/cyan]")
            return

        table = Table(title="Configuration Status", show_header=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Source Type", self.config.source_type)
        table.add_row("Source Path", self.config.source_path)
        table.add_row("Server URL", self.config.server_url)
        table.add_row("Output Dir", self.config.output_dir)
        table.add_row("Total Layers", str(len(self.engine.get_all_layers())))

        models = self.engine.get_models()
        table.add_row("Models", ", ".join(models))

        output_log.write(table)

    def list_layers(self, query: str) -> None:
        """List layers, optionally filtered"""
        output_log = self.query_one("#output-log", RichLog)

        if not self.config or not self.engine:
            output_log.write("[bold red]✗ No configuration loaded. Run: init <source>[/bold red]")
            return

        if query:
            results = self.engine.search(query, limit=20)
            output_log.write(f"[bold]Found {len(results)} layers matching '{query}':[/bold]")

            table = Table(show_header=True)
            table.add_column("Score", style="dim", width=6)
            table.add_column("Layer Name", style="cyan")
            table.add_column("Dimensions", style="yellow")

            for result in results:
                layer = result.layer
                dims = ", ".join(layer.dimensions.keys()) if layer.dimensions else "-"
                layer_name = layer.title or layer.name
                table.add_row(f"{result.score:.0f}", layer_name, dims)

            output_log.write(table)
        else:
            layers = self.engine.get_all_layers()
            output_log.write(f"[bold]Total layers: {len(layers)}[/bold]")

            # Group by model
            models = self.engine.get_models()
            for model in models[:5]:  # Show first 5 models
                model_layers = self.engine.search_by_model(model)
                output_log.write(f"  {model}: {len(model_layers)} layers")

    def fetch_map(self, query: str) -> None:
        """Fetch a map"""
        if not query:
            output_log = self.query_one("#output-log", RichLog)
            output_log.write("[bold red]✗ Please provide a query (e.g., 'map gfs temp')[/bold red]")
            return

        output_log = self.query_one("#output-log", RichLog)
        output_log.write(f"Resolving query: {query}")

        # Resolve query
        try:
            resolved = self.resolver.resolve(query)
        except AmbiguousQueryError as e:
            output_log.write(f"[bold red]✗ {e}[/bold red]")
            if e.alternatives:
                output_log.write("[yellow]Did you mean one of these?[/yellow]")
                for alt in e.alternatives[:5]:
                    output_log.write(f"  • {alt}")
            return
        except Exception as e:
            output_log.write(f"[bold red]✗ Error: {e}[/bold red]")
            return

        layer = resolved.layer
        output_log.write(f"[bold green]✓ Matched layer: {layer.title or layer.name} (confidence: {resolved.confidence:.0f}%)[/bold green]")

        # Show warnings if any
        for warning in resolved.warnings:
            output_log.write(f"[yellow]⚠ {warning}[/yellow]")

        # Build GetMap URL
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetMap',
            'LAYERS': layer.name,
            'STYLES': resolved.style,
            'CRS': resolved.crs,
            'BBOX': ','.join(str(x) for x in resolved.bbox),
            'WIDTH': '800',
            'HEIGHT': '600',
            'FORMAT': self.config.default_format,
        }

        # Add dimensions
        for dim_name, dim_value in resolved.dimensions.items():
            params[dim_name] = dim_value

        # Build URL
        url = self.build_url(self.config.server_url, params)
        output_log.write(f"[dim]URL: {url}[/dim]")

        # Fetch
        output_log.write("Fetching map...")

        try:
            with WMSClient(self.config.server_url) as client:
                response = client.session.get(url, timeout=client.timeout)
                response.raise_for_status()

                # Save image
                filename = self.generate_filename(layer.name, "map", "png")
                filepath = Path(self.config.output_dir) / filename
                filepath.write_bytes(response.content)

                size_kb = len(response.content) / 1024
                output_log.write(f"[bold green]✓ Saved to: {filename} ({size_kb:.1f} KB)[/bold green]")
        except Exception as e:
            output_log.write(f"[bold red]✗ Error fetching map: {e}[/bold red]")

    def fetch_tile(self, query: str) -> None:
        """Fetch a tile"""
        if not query:
            output_log = self.query_one("#output-log", RichLog)
            output_log.write("[bold red]✗ Please provide a query (e.g., 'tile gfs temp')[/bold red]")
            return

        output_log = self.query_one("#output-log", RichLog)
        output_log.write(f"Resolving query: {query}")

        # Resolve query
        try:
            resolved = self.resolver.resolve(query)
        except AmbiguousQueryError as e:
            output_log.write(f"[bold red]✗ {e}[/bold red]")
            if e.alternatives:
                output_log.write("[yellow]Did you mean one of these?[/yellow]")
                for alt in e.alternatives[:5]:
                    output_log.write(f"  • {alt}")
            return
        except Exception as e:
            output_log.write(f"[bold red]✗ Error: {e}[/bold red]")
            return

        layer = resolved.layer
        output_log.write(f"[bold green]✓ Matched layer: {layer.title or layer.name} (confidence: {resolved.confidence:.0f}%)[/bold green]")

        # Show warnings if any
        for warning in resolved.warnings:
            output_log.write(f"[yellow]⚠ {warning}[/yellow]")

        # Random tile coordinates
        import random
        zoom = 5
        max_tile = 2 ** zoom
        tile_x = random.randint(0, max_tile - 1)
        tile_y = random.randint(0, max_tile - 1)

        output_log.write(f"[dim]Random tile: z={zoom}, x={tile_x}, y={tile_y}[/dim]")

        # Build GetGTile URL
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetGTile',
            'LAYERS': layer.name,
            'STYLES': resolved.style,
            'CRS': 'EPSG:4326',
            'ZOOM': str(zoom),
            'TILE_X': str(tile_x),
            'TILE_Y': str(tile_y),
            'FORMAT': self.config.default_format,
        }

        # Add dimensions
        for dim_name, dim_value in resolved.dimensions.items():
            params[dim_name] = dim_value

        # Build URL
        url = self.build_url(self.config.server_url, params)
        output_log.write(f"[dim]URL: {url}[/dim]")

        # Fetch
        output_log.write("Fetching tile...")

        try:
            with WMSClient(self.config.server_url) as client:
                response = client.session.get(url, timeout=client.timeout)
                response.raise_for_status()

                # Save image
                filename = self.generate_filename(layer.name, f"tile_z{zoom}_x{tile_x}_y{tile_y}", "png")
                filepath = Path(self.config.output_dir) / filename
                filepath.write_bytes(response.content)

                size_kb = len(response.content) / 1024
                output_log.write(f"[bold green]✓ Saved to: {filename} ({size_kb:.1f} KB)[/bold green]")
        except Exception as e:
            output_log.write(f"[bold red]✗ Error fetching tile: {e}[/bold red]")

    def fetch_legend(self, query: str) -> None:
        """Fetch a legend"""
        if not query:
            output_log = self.query_one("#output-log", RichLog)
            output_log.write("[bold red]✗ Please provide a query (e.g., 'legend gfs temp')[/bold red]")
            return

        output_log = self.query_one("#output-log", RichLog)
        output_log.write(f"Resolving query: {query}")

        # Resolve query
        try:
            resolved = self.resolver.resolve(query)
        except AmbiguousQueryError as e:
            output_log.write(f"[bold red]✗ {e}[/bold red]")
            if e.alternatives:
                output_log.write("[yellow]Did you mean one of these?[/yellow]")
                for alt in e.alternatives[:5]:
                    output_log.write(f"  • {alt}")
            return
        except Exception as e:
            output_log.write(f"[bold red]✗ Error: {e}[/bold red]")
            return

        layer = resolved.layer
        output_log.write(f"[bold green]✓ Matched layer: {layer.title or layer.name} (confidence: {resolved.confidence:.0f}%)[/bold green]")

        # Show warnings if any
        for warning in resolved.warnings:
            output_log.write(f"[yellow]⚠ {warning}[/yellow]")

        # Build GetLegendGraphic URL
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetLegendGraphic',
            'LAYER': layer.name,
            'STYLE': resolved.style,
            'FORMAT': self.config.default_format,
        }

        # Build URL
        url = self.build_url(self.config.server_url, params)
        output_log.write(f"[dim]URL: {url}[/dim]")

        # Fetch
        output_log.write("Fetching legend...")

        try:
            with WMSClient(self.config.server_url) as client:
                response = client.session.get(url, timeout=client.timeout)
                response.raise_for_status()

                # Save image
                filename = self.generate_filename(layer.name, "legend", "png")
                filepath = Path(self.config.output_dir) / filename
                filepath.write_bytes(response.content)

                size_kb = len(response.content) / 1024
                output_log.write(f"[bold green]✓ Saved to: {filename} ({size_kb:.1f} KB)[/bold green]")
        except Exception as e:
            output_log.write(f"[bold red]✗ Error fetching legend: {e}[/bold red]")

    def build_url(self, base_url: str, params: dict) -> str:
        """Build URL with query parameters"""
        from urllib.parse import urlencode
        query_string = urlencode(params)
        separator = '&' if '?' in base_url else '?'
        return f"{base_url}{separator}{query_string}"

    def generate_filename(self, layer_name: str, suffix: str, extension: str) -> str:
        """Generate descriptive filename"""
        safe_name = layer_name.replace('/', '_').replace(':', '_')
        return f"{safe_name}_{suffix}.{extension}"

    def action_clear(self) -> None:
        """Clear the output log"""
        output_log = self.query_one("#output-log", RichLog)
        output_log.clear()

    def action_reload(self) -> None:
        """Reload configuration"""
        output_log = self.query_one("#output-log", RichLog)
        output_log.write("[bold cyan]Reloading configuration...[/bold cyan]")

        try:
            self.config = load_config()
            caps_xml = get_capabilities_xml(self.config)
            capabilities = parse_capabilities(caps_xml)
            self.engine = FuzzySearchEngine(capabilities)
            self.resolver = SmartQueryResolver(capabilities)

            output_log.write("[bold green]✓ Configuration reloaded successfully[/bold green]")
        except Exception as e:
            output_log.write(f"[bold red]✗ Error reloading configuration: {e}[/bold red]")

    def action_cancel(self) -> None:
        """Cancel current input"""
        query_input = self.query_one("#query-input", Input)
        query_input.value = ""
        self.current_results = []
        self.current_query_prefix = ""
        self.update_suggestions([])

    def get_first_suggestion(self, value: str) -> str | None:
        """Get the first/best suggestion for autocomplete (used by Suggester)."""
        if not value:
            return None

        # Parse command and query
        parts = value.split(maxsplit=1)
        command = parts[0].lower()
        search_query = parts[1] if len(parts) > 1 else ""

        # Command completion
        if len(parts) == 1:
            for cmd in self.commands:
                if cmd.startswith(command):
                    return cmd
            return None

        # Layer name completion
        if not self.engine or not self.resolver:
            return None

        if command in ["map", "tile", "legend", "layers"] and search_query:
            # Use smart resolver to extract dimension tokens before searching
            try:
                extracted_dims, extracted_units, clean_query = self.resolver._extract_tokens(search_query)
            except (AttributeError, TypeError, ValueError):
                # Fallback to original query if extraction fails
                clean_query = search_query

            # Search with clean query (dimensions removed)
            results = self.engine.search(clean_query, limit=1)

            if results:
                best_score = results[0].score

                # Stop suggesting if confidence >= 80%
                if best_score >= 80.0:
                    return None

                layer_name = results[0].layer.title or results[0].layer.name

                # Reconstruct suggestion with original dimension tokens
                suggestion = f"{command} {layer_name}"

                # Add back dimension tokens if they exist
                all_tokens = []
                for dims in extracted_dims.values():
                    all_tokens.extend(dims)
                all_tokens.extend(extracted_units)

                if all_tokens:
                    suggestion += " " + " ".join(all_tokens)

                return suggestion

        return None

    def accept_suggestion(self) -> None:
        """Accept the current inline suggestion from the Suggester"""
        query_input = self.query_one("#query-input", Input)
        current_value = query_input.value

        # Get the suggestion from our get_first_suggestion method
        suggestion = self.get_first_suggestion(current_value)

        if suggestion:
            query_input.value = suggestion
            query_input.cursor_position = len(query_input.value)

            # Clear suggestions panel after accepting
            self.current_results = []
            self.update_suggestions([])


def run_tui():
    """Run the TUI application"""
    app = WMSTui()
    app.run()


if __name__ == "__main__":
    run_tui()
