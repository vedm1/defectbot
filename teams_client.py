"""
Post messages back to the Teams UAT Defects channel via Incoming Webhook.

The Outgoing Webhook (which triggers the Function) can only respond to the
original conversation within 5 seconds. Anything that takes longer must be
posted as a separate message via this Incoming Webhook URL.
"""
import logging
import os

import requests


def post_to_channel(text: str) -> None:
    """Post a plain Markdown message to the configured Teams channel."""
    url = os.environ.get("TEAMS_INCOMING_WEBHOOK_URL")
    if not url:
        raise RuntimeError("TEAMS_INCOMING_WEBHOOK_URL environment variable not set")

    payload = {"text": text}
    r = requests.post(url, json=payload, timeout=10)
    if not r.ok:
        logging.error(
            "Teams incoming webhook returned %s: %s", r.status_code, r.text[:300]
        )
        r.raise_for_status()


def post_adaptive_card(card: dict) -> None:
    """Optional: post a richer Adaptive Card via the incoming webhook."""
    url = os.environ.get("TEAMS_INCOMING_WEBHOOK_URL")
    if not url:
        raise RuntimeError("TEAMS_INCOMING_WEBHOOK_URL environment variable not set")

    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    r = requests.post(url, json=payload, timeout=10)
    if not r.ok:
        logging.error(
            "Teams incoming webhook (card) returned %s: %s",
            r.status_code, r.text[:300],
        )
        r.raise_for_status()
