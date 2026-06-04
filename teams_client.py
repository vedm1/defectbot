"""
Post messages back to Teams via the Workflows-based Incoming Webhook.

The Workflows "Post card in a chat or channel" action expects the request
body to BE an Adaptive Card directly (i.e. the top-level JSON object has
`type: "AdaptiveCard"`). The older Office 365 Connector style `{"text": ...}`
does NOT work with Workflows-flow webhooks — it errors with
"Property 'type' must be 'AdaptiveCard'".

We wrap the formatted message text in a minimal single-TextBlock card.
Adaptive Cards' TextBlock supports a useful markdown subset (bold, italic,
links) which is enough for our reply formatting.
"""
import logging
import os

import requests


def post_to_channel(text: str) -> None:
    """Post a message to the configured Teams channel via Workflows webhook.

    `text` may contain markdown — bold, italic, links. The Adaptive Card
    TextBlock renders the subset Teams supports.
    """
    url = os.environ.get("TEAMS_INCOMING_WEBHOOK_URL")
    if not url:
        raise RuntimeError("TEAMS_INCOMING_WEBHOOK_URL environment variable not set")

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": text,
                "wrap": True,
            }
        ],
    }

    r = requests.post(url, json=card, timeout=10)
    if not r.ok:
        logging.error(
            "Teams incoming webhook returned %s: %s", r.status_code, r.text[:300]
        )
        r.raise_for_status()
