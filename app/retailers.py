"""Optional presentation metadata, kept out of notification rendering logic."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetailerBrand:
    display_name: str
    icon_url: str | None = None
    homepage: str | None = None
    currency: str | None = None
    country: str | None = None


# Adapters remain authoritative for product data. This small catalogue only controls branding.
RETAILER_BRANDS: dict[str, RetailerBrand] = {
    "Loungefly": RetailerBrand("Loungefly", homepage="https://loungefly.com", currency="USD", country="US"),
    "Loungefly UK": RetailerBrand("Loungefly UK", homepage="https://loungefly.co.uk", currency="GBP", country="GB"),
    "Disney Store UK": RetailerBrand("Disney Store UK", homepage="https://www.disneystore.co.uk", currency="GBP", country="GB"),
    "BoxLunch": RetailerBrand("BoxLunch", homepage="https://www.boxlunch.com", currency="USD", country="US"),
    "GeekCore": RetailerBrand("GeekCore", homepage="https://www.geekcore.co.uk", currency="GBP", country="GB"),
}


def retailer_brand(name: str) -> RetailerBrand:
    """Return configured branding or a safe, data-driven fallback."""
    return RETAILER_BRANDS.get(name, RetailerBrand(name))
