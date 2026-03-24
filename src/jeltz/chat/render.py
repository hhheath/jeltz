"""Terminal rendering for jeltz chat."""

from __future__ import annotations

import click

from jeltz.chat.client import AssistantMessageEvent, ChatEvent, ToolCallEvent, ToolResultEvent


def render_event(event: ChatEvent) -> None:
    """Print a chat event to the terminal."""
    if isinstance(event, ToolCallEvent):
        args_str = ", ".join(f"{k}={v!r}" for k, v in event.arguments.items())
        if args_str:
            click.echo(click.style(f"  ⚙ {event.name}({args_str})", dim=True))
        else:
            click.echo(click.style(f"  ⚙ {event.name}", dim=True))

    elif isinstance(event, ToolResultEvent):
        if event.is_error:
            click.echo(click.style(f"  ✗ {event.result}", fg="red", dim=True))
        # Don't print successful results — they're verbose and the LLM will
        # summarize them in its response.

    elif isinstance(event, AssistantMessageEvent):
        click.echo()
        click.echo(event.content)
        click.echo()


def print_banner(device_count: int, tool_count: int, model: str, api_url: str) -> None:
    """Print startup banner."""
    click.echo(click.style(
        f"✓ Connected to {device_count} device(s), exposing {tool_count} tools",
        fg="green",
    ))
    click.echo(click.style(f"✓ LLM: {model} via {api_url}", fg="green"))
    click.echo(click.style("✓ Ready (Ctrl+C to exit)", fg="green"))
    click.echo()


def print_error(message: str) -> None:
    """Print an error message."""
    click.echo(click.style(f"Error: {message}", fg="red"), err=True)
