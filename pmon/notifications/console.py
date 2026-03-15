"""Console/terminal notifications using rich."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pmon.models import StockResult, CheckoutResult, CheckoutStatus
from .base import BaseNotifier

console = Console()


class ConsoleNotifier(BaseNotifier):
    async def notify_in_stock(self, result: StockResult):
        title = Text("IN STOCK!", style="bold green")
        body = Text()
        body.append(f"\n{result.product_name}\n", style="bold white")
        body.append(f"Retailer: ", style="dim")
        body.append(f"{result.retailer}\n", style="cyan")
        if result.price:
            body.append(f"Price: ", style="dim")
            body.append(f"{result.price}\n", style="green")
        body.append(f"URL: ", style="dim")
        body.append(f"{result.url}\n", style="blue underline")

        console.print(Panel(body, title=title, border_style="green"))
        # System bell
        console.bell()

    async def notify_checkout(self, result: CheckoutResult):
        if result.status == CheckoutStatus.SUCCESS:
            style = "green"
            icon = "SUCCESS"
        else:
            style = "red"
            icon = "FAILED"

        title = Text(f"CHECKOUT {icon}", style=f"bold {style}")
        body = Text()
        body.append(f"\n{result.product_name}\n", style="bold white")
        body.append(f"Retailer: ", style="dim")
        body.append(f"{result.retailer}\n", style="cyan")
        if result.order_number:
            body.append(f"Order #: ", style="dim")
            body.append(f"{result.order_number}\n", style="bold green")
        if result.error_message:
            body.append(f"Error: ", style="dim")
            body.append(f"{result.error_message}\n", style="red")

        console.print(Panel(body, title=title, border_style=style))
        console.bell()
