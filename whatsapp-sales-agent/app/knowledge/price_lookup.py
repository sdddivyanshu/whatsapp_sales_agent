"""Exact product matching and price verification.

A price is *quotable* only when :meth:`PriceLookup.check` returns ``VERIFIED``:

* the model code is matched exactly (normalised, token-bounded - ``WC-400`` never
  matches ``WC-400S``);
* if the model has several configurations, the customer's message must identify one;
* all price-list entries for that model/configuration agree (otherwise ``CONFLICT``);
* the entry has a numeric price and an explicit currency;
* it is within ``price_valid_until``; when no validity date exists, the source
  file must be newer than ``PRICE_MAX_AGE_DAYS`` (otherwise ``STALE``).

Tax is never assumed: ``tax_included`` stays ``None`` unless the source says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable

from app.knowledge.product_repository import ProductRepository
from app.models.product import Product, model_key


class PriceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    NOT_FOUND = "NOT_FOUND"
    NO_PRICE = "NO_PRICE"
    AMBIGUOUS = "AMBIGUOUS"
    CONFLICT = "CONFLICT"
    EXPIRED = "EXPIRED"
    STALE = "STALE"


@dataclass
class PriceCheck:
    status: PriceStatus
    requested: str
    product: Product | None = None
    candidates: list[Product] = field(default_factory=list)
    reason: str = ""

    @property
    def quotable(self) -> bool:
        return self.status == PriceStatus.VERIFIED and self.product is not None

    def as_fact(self) -> dict[str, Any]:
        fact: dict[str, Any] = {"model_requested": self.requested, "price_status": self.status.value,
                                "note": self.reason}
        if self.quotable:
            p = self.product
            assert p is not None
            fact.update({
                "product_id": p.product_id, "model": p.model, "configuration": p.configuration,
                "listed_price": p.listed_price, "currency": p.currency,
                "tax": p.tax_treatment(),
                "installation_charges": p.installation_charges or "Unknown",
                "shipping_charges": p.shipping_charges or "Unknown",
                "price_conditions": p.price_conditions or "None stated",
                "price_valid_until": p.price_valid_until or "Not stated",
            })
        elif self.status == PriceStatus.AMBIGUOUS:
            fact["configurations"] = sorted({c.configuration or "standard" for c in self.candidates})
        return fact


def _norm_config(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def text_grams(text: str, max_n: int = 3) -> set[str]:
    tokens = re.findall(r"[A-Za-z0-9]+", text.upper())
    grams: set[str] = set()
    for n in range(1, max_n + 1):
        for i in range(len(tokens) - n + 1):
            grams.add("".join(tokens[i:i + n]))
    return grams


class PriceLookup:
    def __init__(self, repo: ProductRepository, price_max_age_days: int = 180,
                 today: Callable[[], date] = date.today) -> None:
        self.repo = repo
        self.price_max_age_days = price_max_age_days
        self.today = today

    def match_models(self, text: str) -> list[str]:
        """Model keys explicitly mentioned in ``text`` (exact, token-bounded)."""
        tokens = re.findall(r"[A-Za-z0-9]+", text.upper())
        spans: dict[str, list[tuple[int, int]]] = {}
        for n in range(1, 4):
            for i in range(len(tokens) - n + 1):
                spans.setdefault("".join(tokens[i:i + n]), []).append((i, i + n))
        matches: dict[str, list[tuple[int, int]]] = {}
        for product in self.repo.all_products():
            if not product.key:
                continue
            for candidate in (product.key, model_key(product.product_id)):
                if len(candidate) >= 3 and candidate in spans:
                    matches.setdefault(product.key, []).extend(spans[candidate])
        # "WC 400 S" contains both "WC400" (tokens 0-2) and "WC400S" (tokens 0-3):
        # a match whose every span lies inside a longer match's span is dropped.
        result: list[str] = []
        for key, key_spans in matches.items():
            covered = all(
                any(o_key != key and o[0] <= s[0] and s[1] <= o[1] and (o[1] - o[0]) > (s[1] - s[0])
                    for o_key, o_spans in matches.items() for o in o_spans)
                for s in key_spans)
            if not covered:
                result.append(key)
        return result

    def products_for_text(self, text: str, limit: int = 6) -> list[Product]:
        """Products whose category / name words appear in ``text`` (for context only)."""
        words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3}
        stop = {"machine", "machines", "the", "and", "for", "with", "price", "need", "want"}
        words -= stop
        scored: list[tuple[int, Product]] = []
        for p in self.repo.all_products():
            hay = set(re.findall(r"[a-z0-9]+", " ".join(
                filter(None, [p.category, p.product_name, p.model, p.applications])).lower()))
            score = len(words & hay)
            if score:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        seen: set[tuple[str, str]] = set()
        out: list[Product] = []
        for _, p in scored:
            ident = (p.key, _norm_config(p.configuration))
            if ident not in seen:
                seen.add(ident)
                out.append(p)
            if len(out) >= limit:
                break
        return out

    def check(self, key: str, message_text: str = "") -> PriceCheck:
        products = self.repo.by_model_key(key) or self.repo.by_product_id(key)
        if not products:
            return PriceCheck(PriceStatus.NOT_FOUND, key, reason="model not in price list")

        configs = {_norm_config(p.configuration) for p in products}
        if len(configs) > 1:
            text = message_text.lower()
            chosen = [c for c in configs if c and all(w in text for w in c.split())]
            if len(chosen) != 1:
                return PriceCheck(PriceStatus.AMBIGUOUS, key, candidates=products,
                                  reason="several configurations exist; ask which one")
            products = [p for p in products if _norm_config(p.configuration) == chosen[0]]

        priced = [p for p in products if p.listed_price is not None]
        if not priced:
            return PriceCheck(PriceStatus.NO_PRICE, key, product=products[0], candidates=products,
                              reason="no numeric listed price in the price list")
        variants = {(p.listed_price, (p.currency or "").upper(), p.tax_included) for p in priced}
        if len(variants) > 1:
            sources = sorted({f"{p.source_file} ({p.source_reference})" for p in priced})
            return PriceCheck(PriceStatus.CONFLICT, key, candidates=priced,
                              reason="conflicting price-list entries: " + "; ".join(sources))
        product = priced[0]
        if not product.currency:
            return PriceCheck(PriceStatus.NO_PRICE, key, product=product,
                              reason="currency not stated in price list")
        today = self.today()
        if product.price_valid_until:
            if date.fromisoformat(product.price_valid_until) < today:
                return PriceCheck(PriceStatus.EXPIRED, key, product=product,
                                  reason=f"price expired on {product.price_valid_until}")
        else:
            mtime = self.repo.source_file_mtimes().get(product.source_file)
            if mtime:
                age = (today - datetime.fromisoformat(mtime).date()).days
                if age > self.price_max_age_days:
                    return PriceCheck(PriceStatus.STALE, key, product=product,
                                      reason=f"price list is {age} days old with no validity date")
        return PriceCheck(PriceStatus.VERIFIED, key, product=product, candidates=priced,
                          reason=f"source: {product.source_file} ({product.source_reference})")
