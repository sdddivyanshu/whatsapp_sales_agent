"""Normalised product / price-list record."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel


def format_amount(value: float, currency: str | None) -> str:
    """Indian digit grouping for INR (12,50,000), western grouping otherwise."""
    whole = int(round(value))
    if (currency or "").upper() == "INR":
        s = str(whole)
        if len(s) > 3:
            head, tail = s[:-3], s[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            if head:
                groups.insert(0, head)
            s = ",".join(groups) + "," + tail
        return f"₹{s}"
    return f"{currency or ''} {whole:,}".strip()


def model_key(value: str | None) -> str:
    """Normalise a model code for exact comparison: 'WC-400 S' -> 'WC400S'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


class Product(BaseModel):
    product_id: str | None = None
    product_name: str | None = None
    category: str | None = None
    model: str | None = None
    configuration: str | None = None
    description: str | None = None
    technical_specifications: str | None = None
    applications: str | None = None
    listed_price: float | None = None
    currency: str | None = None
    tax_included: bool | None = None  # None == unknown; never assumed
    tax_rate: float | None = None
    installation_charges: str | None = None
    shipping_charges: str | None = None
    warranty: str | None = None
    availability: str | None = None
    minimum_order_quantity: str | None = None
    price_valid_until: str | None = None  # ISO date
    price_conditions: str | None = None
    source_file: str = ""
    source_reference: str | None = None
    last_updated_at: str | None = None

    @property
    def key(self) -> str:
        return model_key(self.model)

    @property
    def display_name(self) -> str:
        name = self.product_name or self.model or self.product_id or "product"
        if self.model and self.model not in name:
            name = f"{name} ({self.model})"
        if self.configuration:
            name = f"{name} - {self.configuration}"
        return name

    def tax_treatment(self) -> str:
        if self.tax_included is True:
            rate = f" @ {self.tax_rate:g}%" if self.tax_rate is not None else ""
            return f"GST included{rate}"
        if self.tax_included is False:
            rate = f" @ {self.tax_rate:g}%" if self.tax_rate is not None else ""
            return f"GST extra{rate}"
        return "GST treatment not specified in price list"

    def facts_for_prompt(self, include_price: bool) -> dict[str, Any]:
        data = {
            "product_id": self.product_id,
            "name": self.product_name,
            "model": self.model,
            "configuration": self.configuration,
            "category": self.category,
            "description": self.description,
            "technical_specifications": self.technical_specifications,
            "applications": self.applications,
            "warranty": self.warranty,
            "availability": self.availability,
            "installation_charges": self.installation_charges,
            "shipping_charges": self.shipping_charges,
            "minimum_order_quantity": self.minimum_order_quantity,
        }
        if include_price:
            data["listed_price"] = self.listed_price
            data["currency"] = self.currency
            data["tax"] = self.tax_treatment()
            data["price_conditions"] = self.price_conditions
            data["price_valid_until"] = self.price_valid_until
        return {k: (v if v not in (None, "") else "Unknown") for k, v in data.items()}
