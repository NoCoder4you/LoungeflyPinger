from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.config import PriceAlertConfig
from app.database import Database
from app.http import HttpClientError, HttpErrorKind
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.gwens_mermaid_cove import GwensMermaidCoveMonitor
from app.monitors.shopify import ShopifyParseError
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService

FIXTURE = Path(__file__).parent / "fixtures/gwens_mermaid_cove/products.json"

class FakeHttp:
    def __init__(self, responses): self.responses, self.urls = responses, []
    async def get_json(self, url):
        self.urls.append(url)
        value = self.responses[url] if isinstance(self.responses, dict) else self.responses.pop(0)
        if isinstance(value, Exception): raise value
        return value

class Recorder(NotificationProvider):
    def __init__(self): self.alerts=[]
    async def send(self, alert): self.alerts.append(alert); return True

def raws(): return json.loads(FIXTURE.read_text())["products"]
def parse(index, *, collections=("backpacks",), signals=()):
    raw=raws()[index]; monitor=GwensMermaidCoveMonitor(FakeHttp([]))
    return monitor.parse_product(raw, product_type=monitor.classify_product_type(raw),
                                 collections=set(collections), signals=set(signals))

@pytest.mark.parametrize(("index","kind"), [(0,"Mini Backpack"),(1,"Full Size Backpack"),(2,"Convertible Bag"),(3,"Crossbody"),(4,"Tote"),(5,"Handbag")])
def test_all_primary_bag_types(index,kind): assert parse(index).product_type==kind

@pytest.mark.parametrize("index",[6,7,8,9])
def test_non_bag_accessories_are_excluded(index):
    assert GwensMermaidCoveMonitor.classify_product_type(raws()[index]) is None

def test_loungefly_evidence_is_required_even_in_backpacks():
    monitor=GwensMermaidCoveMonitor(FakeHttp([]))
    assert monitor.is_loungefly(raws()[0])
    assert not monitor.is_loungefly(raws()[10])

@pytest.mark.parametrize(("index","status"), [(0,Availability.IN_STOCK),(11,Availability.OUT_OF_STOCK),(15,Availability.PREORDER),(16,Availability.COMING_SOON),(18,Availability.LOW_STOCK)])
def test_normalized_availability(index,status): assert parse(index).availability==status

def test_description_refines_generic_backpack_when_shopify_type_is_empty():
    assert parse(11).product_type == "Mini Backpack"

def test_variant_unavailable_and_explicit_sold_out_override_add_to_cart():
    product=parse(11)
    assert "Add to Cart" in raws()[11]["body_html"]
    assert product.availability==Availability.OUT_OF_STOCK

def test_structured_identity_metadata_price_and_sale():
    product=parse(17,collections=("backpacks","shop-all","70-clearance"),signals=("new_arrival","last_chance"))
    assert (product.retailer_product_id,product.variant_id)==("18","180")
    assert product.sku=="LF-18" and product.barcode=="0671803000018"
    assert product.vendor=="Gwen's Mermaid Cove" and product.tags==("Loungefly",)
    assert product.url.endswith("/products/product-18")
    assert product.price==Decimal("75.00") and product.compare_at_price==Decimal("100.00")
    assert product.sale and product.last_chance

@pytest.mark.parametrize(("index","retailer","region","limited_edition","limited_release"),[(12,"Gwen's Mermaid Cove",None,True,False),(13,None,None,False,True),(14,None,"EU",False,False)])
def test_exclusive_origin_and_limited_metadata(index,retailer,region,limited_edition,limited_release):
    product=parse(index,collections=("exclusive",),signals=("exclusive_collection",))
    assert product.exclusive and product.exclusive_retailer==retailer
    assert product.exclusive_region==region
    assert product.limited_edition is limited_edition and product.limited_release is limited_release

def test_publication_new_arrivals_and_release_date_are_distinct():
    listing=parse(0,collections=("shop-all",),signals=("new_arrival",))
    assert listing.listing_published_at.date()==date(2026,8,1) and listing.release is None
    release=parse(19)
    assert release.release.precision==ReleasePrecision.EXACT_DATETIME
    assert release.release.release_date==date(2026,9,20)

def test_preorder_eta_is_not_release_date():
    product=parse(15)
    assert product.estimated_arrival_date==date(2026,10,2) and product.release is None

@pytest.mark.asyncio
async def test_complete_pagination_newest_sampling_overlap_and_non_loungefly_filter():
    monitor=GwensMermaidCoveMonitor(FakeHttp([])); monitor.page_size=2
    monitor.collections={"backpacks":None,"shop-all":"new_arrival"}; monitor.collection_page_limits={"shop-all":1}
    monitor.http.responses=[{"products":raws()[:2]},{"products":[raws()[2]]},{"products":[raws()[0],raws()[10]]}]
    products=await monitor.discover_products()
    assert len(products)==3 and len({p.retailer_product_id for p in products})==3
    assert next(p for p in products if p.retailer_product_id=="1").collections==("backpacks","shop-all")
    assert "page=2" in monitor.http.urls[1] and len(monitor.http.urls)==3

@pytest.mark.parametrize("mutation",[lambda x:x.update(variants=[]),lambda x:x["variants"][0].pop("price"),lambda x:x["variants"][0].pop("available")])
def test_missing_commerce_data_fails_closed(mutation):
    raw=json.loads(json.dumps(raws()[0])); mutation(raw); monitor=GwensMermaidCoveMonitor(FakeHttp([]))
    with pytest.raises(ShopifyParseError): monitor.parse_product(raw,product_type="Mini Backpack",collections=set(),signals=set())

@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind",[(429,HttpErrorKind.RATE_LIMITED),(500,HttpErrorKind.SERVER)])
async def test_http_and_parser_failures_preserve_previous_state(status,kind):
    product=parse(0); error=HttpClientError(kind,"failure",status); monitor=GwensMermaidCoveMonitor(FakeHttp([error,error]))
    assert not await monitor.health_check()
    checked=await monitor.check_product(product)
    assert checked.availability==Availability.ERROR and checked.price==product.price

async def result(value): return value

@pytest.mark.asyncio
async def test_initial_sync_new_listing_restock_price_drop_and_restart_dedup(tmp_path):
    path=tmp_path/"gwen.db"; recorder=Recorder(); sold=parse(11)
    async with Database(path) as db:
        service=MonitorService(type("M",(),{"discover_products":lambda self:result([sold])})(),db,recorder,retailer_name="Gwen's Mermaid Cove",price_alerts=PriceAlertConfig(minimum_drop_percent=10,minimum_drop_value=5))
        assert await service.synchronize()==[]
        added=parse(0); service.monitor=type("M",(),{"discover_products":lambda self:result([sold,added])})()
        assert [a.alert_type for a in await service.synchronize()]==[AlertType.NEW_PRODUCT]
        changed=replace(sold,availability=Availability.IN_STOCK,price=Decimal("75")); service.monitor=type("M",(),{"discover_products":lambda self:result([changed,added])})()
        assert {a.alert_type for a in await service.synchronize()}=={AlertType.RESTOCK,AlertType.PRICE_DROP}
    async with Database(path) as db:
        service=MonitorService(type("M",(),{"discover_products":lambda self:result([changed,added])})(),db,Recorder(),retailer_name="Gwen's Mermaid Cove")
        assert await service.synchronize()==[]
