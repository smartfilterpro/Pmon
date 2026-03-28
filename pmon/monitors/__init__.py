"""Stock monitors for various retailers."""

from .base import BaseMonitor
from .pokemoncenter import PokemonCenterMonitor
from .target import TargetMonitor
from .bestbuy import BestBuyMonitor
from .walmart import WalmartMonitor
from .costco import CostcoMonitor
from .samsclub import SamsClubMonitor
from .amazon import AmazonMonitor
from .redsky_poller import RedSkyPoller, RedSkyProductData, RedSkySearch, SearchResult
from .bestbuy_search import BestBuySearch

MONITORS: dict[str, type[BaseMonitor]] = {
    "pokemoncenter": PokemonCenterMonitor,
    "target": TargetMonitor,
    "bestbuy": BestBuyMonitor,
    "walmart": WalmartMonitor,
    "costco": CostcoMonitor,
    "samsclub": SamsClubMonitor,
    "amazon": AmazonMonitor,
}


def get_monitor(retailer: str) -> type[BaseMonitor]:
    monitor_class = MONITORS.get(retailer)
    if not monitor_class:
        raise ValueError(f"No monitor for retailer: {retailer}")
    return monitor_class
