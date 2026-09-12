from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.config import PriceAlertConfig, ReleaseAlertConfig
from app.database import Database
from app.http import HttpClientError, HttpErrorKind
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.pop_pelican import PopPelicanMonitor
from app.monitors.shopify import ShopifyParseError, ShopifyRetailerMonitor
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService

FIXTURE = Path(__file__).parent / "fixtures/pop_pelican/products.json"

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
def parse(raw):
    monitor=PopPelicanMonitor(FakeHttp([]))
    kind=monitor.classify_product_type(raw)
    return monitor.parse_product(raw, product_type=kind, collections={"loungefly-bags"}, signals=set())

def raw(**changes):
    value=json.loads(json.dumps(raws()[0])); value.update(changes); return value

@pytest.mark.parametrize(("index","expected"), [(0,"Mini Backpack"),(1,"Backpack"),(2,"Crossbody"),(3,"Tote"),(4,"Convertible Bag"),(5,"Other Bag")])
def test_enabled_bag_classification(index, expected):
    assert PopPelicanMonitor.classify_product_type(raws()[index]) == expected

@pytest.mark.parametrize("index", [6,7,8,9])
def test_accessories_are_excluded(index):
    assert PopPelicanMonitor.classify_product_type(raws()[index]) is None

@pytest.mark.parametrize(("index","expected"), [(0,Availability.IN_STOCK),(11,Availability.OUT_OF_STOCK),(12,Availability.LOW_STOCK),(10,Availability.PREORDER),(13,Availability.COMING_SOON)])
def test_availability(index, expected): assert parse(raws()[index]).availability == expected

def test_identity_aud_pricing_and_structured_metadata():
    product=parse(raws()[16])
    assert (product.retailer,product.currency)==("Pop Pelican","AUD")
    assert (product.retailer_product_id,product.variant_id)==("17","170")
    assert product.sku=="LOU-17" and product.vendor=="Pop Pelican"
    assert product.tags==("Loungefly","Disney") and product.url.endswith('/products/product-17')
    assert product.price==Decimal('99.95') and product.compare_at_price==Decimal('149.95')
    assert product.original_price==Decimal('149.95') and product.sale

def test_eta_publication_and_official_release_are_separate():
    preorder=parse(raws()[10])
    assert preorder.estimated_arrival_text=='ETA: October 2026'
    assert preorder.estimated_arrival_date is None and preorder.release is None
    assert preorder.listing_published_at.date()==date(2026,8,1)
    released=parse(raw(description='Release Date: 20 September 2026. ETA: October 2026'))
    assert released.release.precision==ReleasePrecision.DATE_ONLY
    assert released.release.release_date==date(2026,9,20)

def test_external_regional_and_retailer_exclusives_are_not_confused():
    external,regional,retailer=map(parse,(raws()[14],raws()[15],raws()[16]))
    assert external.exclusive and external.exclusive_retailer is None
    assert regional.exclusive and regional.exclusive_region=='AU' and regional.exclusive_retailer is None
    assert retailer.exclusive and retailer.exclusive_retailer=='Pop Pelican'

@pytest.mark.asyncio
async def test_pagination_overlap_and_accessory_suppression():
    monitor=PopPelicanMonitor(FakeHttp([])); monitor.page_size=10
    first=raws()[:10]; second=raws()[10:]
    monitor.http.responses=[{'products':first},{'products':second},{'products':first[:1]}]
    products=await monitor.discover_products()
    assert len(products)==13 and len({p.retailer_product_id for p in products})==13
    duplicate=next(p for p in products if p.retailer_product_id=='1')
    assert duplicate.collections==('loungefly-bags','new-arrivals')
    assert 'page=2' in monitor.http.urls[1]

@pytest.mark.parametrize('value',[None,{}, {'products':'bad'},{'products':[None]}])
def test_malformed_json(value):
    if isinstance(value,dict) and 'products' in value:
        with pytest.raises(ShopifyParseError): ShopifyRetailerMonitor._product_list(value)
    else:
        with pytest.raises((ShopifyParseError,TypeError)):
            PopPelicanMonitor(FakeHttp([])).parse_product(value,product_type='Mini Backpack',collections=set(),signals=set())

@pytest.mark.parametrize('mutation',[lambda x:x.update(variants=[]),lambda x:x['variants'][0].pop('price'),lambda x:x['variants'][0].pop('available')])
def test_missing_commerce_fields_fail_closed(mutation):
    value=raw(); mutation(value)
    with pytest.raises(ShopifyParseError): parse(value)

@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind',[(429,HttpErrorKind.RATE_LIMITED),(500,HttpErrorKind.SERVER)])
async def test_http_errors_and_parser_failure_preserve_prior_state(status,kind):
    product=parse(raws()[0]); error=HttpClientError(kind,'failure',status)
    monitor=PopPelicanMonitor(FakeHttp([error,error]))
    assert not await monitor.health_check()
    checked=await monitor.check_product(product)
    assert checked.availability==Availability.ERROR and checked.price==product.price

async def result(value): return value

@pytest.mark.asyncio
async def test_initial_sync_restart_and_change_events(tmp_path):
    path=tmp_path/'pop.db'; recorder=Recorder()
    base=parse(raws()[11])
    async with Database(path) as db:
        monitor=type('M',(),{'discover_products':lambda self:result([base])})()
        service=MonitorService(monitor,db,recorder,retailer_name='Pop Pelican',price_alerts=PriceAlertConfig(minimum_drop_percent=10,minimum_drop_value=5),release_alerts=ReleaseAlertConfig(notify_existing_on_upgrade=True))
        assert await service.synchronize()==[]
        new=parse(raws()[10]); service.monitor=type('M',(),{'discover_products':lambda self:result([base,new])})()
        assert [a.alert_type for a in await service.synchronize()]==[AlertType.NEW_PRODUCT]
        changed=replace(base,availability=Availability.IN_STOCK,price=Decimal('99.95'),release=parse(raw(description='Release Date: 20 September 2026')).release)
        service.monitor=type('M',(),{'discover_products':lambda self:result([changed,new])})()
        assert {a.alert_type for a in await service.synchronize()}>={AlertType.RESTOCK,AlertType.PRICE_DROP,AlertType.RELEASE_DATE_FOUND}
        opened=replace(new,availability=Availability.OUT_OF_STOCK,preorder=False)
        service.monitor=type('M',(),{'discover_products':lambda self:result([changed,opened])})(); await service.synchronize()
        reopened=replace(opened,availability=Availability.PREORDER,preorder=True,estimated_arrival_text='ETA: November 2026')
        service.monitor=type('M',(),{'discover_products':lambda self:result([changed,reopened])})()
        assert {a.alert_type for a in await service.synchronize()}=={AlertType.PREORDER_OPEN,AlertType.ETA_CHANGED}
        changed_release=replace(changed,release=parse(raw(description='Release Date: 21 September 2026')).release)
        service.monitor=type('M',(),{'discover_products':lambda self:result([changed_release,reopened])})()
        assert AlertType.RELEASE_DATE_CHANGED in {a.alert_type for a in await service.synchronize()}
    async with Database(path) as db:
        service=MonitorService(type('M',(),{'discover_products':lambda self:result([changed_release,reopened])})(),db,Recorder(),retailer_name='Pop Pelican')
        assert await service.synchronize()==[]
