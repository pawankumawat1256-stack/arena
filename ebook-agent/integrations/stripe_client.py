"""Stripe: Checkout sessions for paid products, webhook signature checks, and fee lookups."""

from __future__ import annotations

import json
import logging
from typing import Any

import stripe

from config import settings

logger = logging.getLogger(__name__)


class StripeError(RuntimeError):
    """Stripe is not configured, a webhook signature is invalid, or an API call failed."""


def _configure() -> None:
    if not settings.stripe_secret_key:
        raise StripeError("STRIPE_SECRET_KEY is not set")
    stripe.api_key = settings.stripe_secret_key
    stripe.max_network_retries = 2


def verify_webhook(payload: bytes, signature_header: str | None) -> dict[str, Any]:
    """Check the Stripe-Signature header and return the parsed event. Raises StripeError when it is invalid."""
    if not settings.stripe_webhook_secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET is not set")
    if not signature_header:
        raise StripeError("Missing Stripe-Signature header")
    try:
        stripe.Webhook.construct_event(payload, signature_header, settings.stripe_webhook_secret)
    except ValueError as exc:
        raise StripeError("Invalid webhook payload") from exc
    except stripe.SignatureVerificationError as exc:
        raise StripeError("Invalid webhook signature") from exc
    event = json.loads(payload)
    if not isinstance(event, dict) or not event.get("id"):
        raise StripeError("Webhook payload is not a Stripe event")
    return event


def create_checkout_url(
    *,
    product_id: int,
    ebook_id: int,
    product_name: str,
    price_cents: int,
    success_url: str,
    cancel_url: str,
) -> str:
    _configure()
    metadata = {"product_id": str(product_id), "ebook_id": str(ebook_id)}
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(price_cents),
                        "product_data": {"name": product_name[:250]},
                    },
                }
            ],
            metadata=metadata,
            payment_intent_data={"metadata": metadata},
            customer_creation="if_required",
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except stripe.StripeError as exc:
        raise StripeError(f"Stripe could not create a checkout session: {exc.user_message or exc}") from exc
    url = session.get("url")
    if not url:
        raise StripeError("Stripe returned a checkout session without a URL")
    return str(url)


def fetch_fee_and_net(payment_intent_id: str) -> tuple[int, int] | None:
    """Stripe's fee and net for a payment, in cents. Returns None if the balance transaction is unavailable."""
    _configure()
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id, expand=["latest_charge.balance_transaction"])
    except stripe.StripeError as exc:
        logger.warning("Could not read Stripe fees for %s: %s", payment_intent_id, exc)
        return None
    charge = intent.get("latest_charge")
    balance = charge.get("balance_transaction") if isinstance(charge, dict) else None
    if not isinstance(balance, dict):
        return None
    return int(balance.get("fee") or 0), int(balance.get("net") or 0)
