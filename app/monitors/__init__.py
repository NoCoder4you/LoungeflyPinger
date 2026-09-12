"""Retailer monitor adapters."""

from .base import RetailerMonitor
from .boxlunch import BoxLunchMonitor, HotTopicUSMonitor
from .cm_pop import CMPopMonitor
from .circle_of_hope import CircleOfHopeMonitor
from .cordys_corner import CordysCornerMonitor
from .cool_merch import CoolMerchMonitor
from .damaged_society import DamagedSocietyMonitor
from .disney_store_uk import DisneyStoreUKMonitor, DisneyStoreUSMonitor
from .entertainment_earth import EntertainmentEarthMonitor
from .forbidden_planet import ForbiddenPlanetMonitor
from .emp import EMPMonitor, EMPRegion, EMP_REGIONS
from .geekcore import GeekCoreMonitor
from .geek_garage import GeekGarageMonitor
from .get_ready_comics import GetReadyComicsMonitor
from .gwens_mermaid_cove import GwensMermaidCoveMonitor
from .infinity_collectables import InfinityCollectablesMonitor
from .koolaz import KoolazMonitor
from .lf_lovers import LFLoversMonitor
from .loungefly_uk import LoungeflyCanadaMonitor, LoungeflyUKMonitor, LoungeflyUSMonitor
from .modern_pinup import ModernPinUpMonitor
from .ozzie_collectables import OzzieCollectablesMonitor
from .merchoid_uk import MerchoidUKMonitor
from .magic_madhouse import MagicMadhouseMonitor
from .pink_a_la_mode import PinkALaModeMonitor
from .popcultcha import PopcultchaMonitor
from .pop_pelican import PopPelicanMonitor
from .razmatazz import RazmatazzMonitor
from .something_different import SomethingDifferentMonitor
from .shopify import ShopifyRetailerMonitor
from .street_707 import Street707Monitor
from .truffleshuffle import TruffleShuffleMonitor
from .world_1_1_games import World11GamesMonitor

__all__ = [
    "BoxLunchMonitor", "CircleOfHopeMonitor", "CMPopMonitor", "CoolMerchMonitor", "CordysCornerMonitor", "DamagedSocietyMonitor", "DisneyStoreUKMonitor", "DisneyStoreUSMonitor",
    "EntertainmentEarthMonitor", "EMPMonitor", "EMPRegion", "EMP_REGIONS", "ForbiddenPlanetMonitor", "GeekCoreMonitor", "GeekGarageMonitor", "GetReadyComicsMonitor", "HotTopicUSMonitor",
    "InfinityCollectablesMonitor", "KoolazMonitor", "LFLoversMonitor",
    "LoungeflyCanadaMonitor", "LoungeflyUKMonitor",
    "LoungeflyUSMonitor", "MagicMadhouseMonitor", "MerchoidUKMonitor", "ModernPinUpMonitor", "OzzieCollectablesMonitor", "PinkALaModeMonitor", "PopcultchaMonitor", "RetailerMonitor",
    "PopPelicanMonitor", "RazmatazzMonitor", "ShopifyRetailerMonitor", "SomethingDifferentMonitor", "Street707Monitor", "TruffleShuffleMonitor",
    "World11GamesMonitor", "GwensMermaidCoveMonitor",
]
