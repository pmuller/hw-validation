from __future__ import annotations

from rich.console import Console

console = Console()
error_console = Console(stderr=True)


def info(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def warning(message: str) -> None:
    error_console.print(f"[yellow]{message}[/yellow]")


def failure(message: str) -> None:
    error_console.print(f"[red]{message}[/red]")
