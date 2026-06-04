"""
UAT Defect Triage Agent — FastAPI entry point for Render (or any container/VM host).

Endpoints:
  POST /api/defect  — Teams Outgoing Webhook callback. Validates HMAC,
                       schedules background work, acks within Teams' 5-second
                       response window.
  GET  /healthz     — liveness probe. Render hits this; UptimeRobot can ping
                       it every 10 minutes to keep the free instance warm.

Background work runs in-process via FastAPI's BackgroundTasks — no Storage Queue,
no Service Bus. Trade-off: if the process restarts mid-task, that one defect is
lost. Fine for PoC.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from ado_client import health_check as ado_health
from claude_client import triage_defect
from teams_client import post_to_channel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("defect-agent")

app = FastAPI(
    title="UAT Defect Triage Agent",
    description="Teams Outgoing Webhook -> Claude triage -> ADO Bug under Epic #375255",
    version="0.1.0",
)


# ----------------------------------------------------------------------------
# Health probe
# ----------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    """Cheap liveness probe — no external calls.

    Note: deliberately NOT /healthz. Google Front End appears to intercept
    that exact path on Cloud Run and return its own 404 before the request
    reaches the container.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict:
    """Readiness probe — verifies the ADO PAT works and the Epic is reachable."""
    try:
        info = ado_health()
        return {"status": "ready", "ado": info}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"ADO unreachable: {e}")


# ----------------------------------------------------------------------------
# Teams webhook
# ----------------------------------------------------------------------------
@app.post("/api/defect")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    """Receive a defect submission.

    Supports two auth paths simultaneously (during transition):
      1. Power Automate Workflows (new path) — header `X-Agent-Token` must match
         the AGENT_SHARED_SECRET env var. Payload is a structured JSON object
         with messageText, reporter info, and base64-encoded attachments.
      2. Teams Outgoing Webhook (legacy) — `Authorization: HMAC <signature>`
         signed against TEAMS_WEBHOOK_SECRET. Payload is the raw Teams shape.
    """
    body_bytes = await request.body()

    # 1a. Workflow auth (preferred for new submissions)
    agent_token = request.headers.get("x-agent-token", "")
    expected_token = os.environ.get("AGENT_SHARED_SECRET", "")
    workflow_authed = bool(
        expected_token
        and agent_token
        and hmac.compare_digest(expected_token.encode(), agent_token.encode())
    )

    # 1b. Outgoing Webhook auth (legacy fallback)
    hmac_authed = False
    if not workflow_authed:
        secret_b64 = os.environ.get("TEAMS_WEBHOOK_SECRET", "")
        auth_header = request.headers.get("authorization", "")
        hmac_authed = _validate_hmac(body_bytes, secret_b64, auth_header)

    if not (workflow_authed or hmac_authed):
        log.warning(
            "Auth failed | x_agent_token_present=%s | hmac_present=%s",
            bool(agent_token), bool(request.headers.get("authorization")),
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = json.loads(body_bytes)
    except Exception:
        return _ack("Sorry, I couldn't read that message. Please try again.")

    # 2. Extract submission details from whichever payload shape we got.
    if workflow_authed:
        log.info("Submission via Workflows path")
        text, reporter_name, reporter_email, attachments = _parse_workflow_payload(payload)
    else:
        log.info("Submission via Outgoing Webhook path")
        _log_payload_structure(payload)
        text = _extract_message_text(payload)
        reporter = payload.get("from", {}) or {}
        reporter_name = reporter.get("name") or "Unknown reporter"
        reporter_email = reporter.get("aadObjectId") or ""
        attachments = _extract_attachments(payload)

    if len(text) < 10:
        return _ack(
            "Please include defect details with your `@DefectBot` mention. "
            "Example: *Excel export fails on Market Sizes dashboard — 500 error "
            "when filtering all geographies, UAT Chrome 124.*"
        )

    # Run triage SYNCHRONOUSLY so the response itself carries the ADO ticket ID.
    # Takes ~10s end-to-end (Claude + ADO). Teams may show a brief
    # "still working" indicator past 5s, but the response lands and the
    # ticket is created regardless.
    log.info(
        "triage start | reporter=%s | text_len=%d | attachments=%d",
        reporter_name, len(text), len(attachments),
    )

    try:
        result = triage_defect(
            user_text=text,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            attachments=attachments,
        )
        message = _format_success(result)
        log.info(
            "triage success | bug=%s | related=%s",
            result.get("bugId"), result.get("relatedBugId"),
        )
    except Exception as e:
        log.exception("triage failed")
        message = (
            f"Triage failed for **{reporter_name}**'s defect: "
            f"`{str(e)[:200]}`. A human will follow up."
        )

    # Best-effort: also post to the UAT Defects channel so the triage team
    # has an at-a-glance view, even when the original @-mention was elsewhere.
    # Failure here doesn't fail the request — the user's reply is the source of truth.
    try:
        post_to_channel(message)
    except Exception:
        log.exception("Failed to post result to UAT Defects channel (continuing)")

    return _ack(message)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _validate_hmac(body: bytes, secret_b64: str, auth_header: str) -> bool:
    """Teams Outgoing Webhook signs the raw body with HMAC-SHA256."""
    if not secret_b64 or not auth_header.startswith("HMAC "):
        return False
    received = auth_header[5:]
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    computed = base64.b64encode(
        hmac.new(key, body, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(computed, received)


def _strip_mentions(text: str) -> str:
    """Remove Teams <at>BotName</at> mention tags from the message text."""
    return re.sub(r"<at>.*?</at>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()


def _parse_workflow_payload(payload: dict) -> tuple[str, str, str, list[dict]]:
    """Parse the structured payload sent by the Power Automate Workflows flow.

    Expected shape:
        {
            "messageText": "Sidebar - bg colour wrong\\n- font too small\\n...",
            "reporterName": "Ved Muthal",
            "reporterEmail": "ved@euromonitor.com",
            "attachments": [
                {
                    "name": "screenshot.png",
                    "contentType": "image/png",
                    "contentBase64": "iVBORw0KGgo..."
                }
            ]
        }

    Returns (text, reporter_name, reporter_email, attachments) where
    attachments is a list of {name, url, content} dicts compatible with
    triage_defect's existing signature (url is empty since Workflows sends
    decoded bytes directly).
    """
    text = (payload.get("messageText") or "").strip()
    reporter_name = payload.get("reporterName") or "Unknown reporter"
    reporter_email = payload.get("reporterEmail") or ""

    attachments: list[dict] = []
    for a in (payload.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("contentType") or "attachment"
        b64 = a.get("contentBase64") or ""
        content: bytes | None = None
        if b64:
            try:
                content = base64.b64decode(b64)
                log.info(
                    "Workflow attachment decoded | name=%s | bytes=%d",
                    name, len(content),
                )
            except Exception:
                log.exception(
                    "Workflow attachment b64 decode failed | name=%s", name
                )
        attachments.append({"name": name, "url": "", "content": content})

    return text, reporter_name, reporter_email, attachments


def _extract_message_text(payload: dict) -> str:
    """Extract the user's defect text from a Teams webhook payload.

    Two cases:
      * Plain-text messages → body is in payload['text'].
      * Rich-formatted messages (bullets, paragraphs) → Teams puts the
        rendered HTML in attachments[0].content with contentType=text/html
        and contentUrl=null. payload['text'] then contains only the bare
        @mention markup. We detect that case and parse the HTML.

    Either way we return readable plain text with mention markup stripped.
    """
    plain = _strip_mentions(payload.get("text", "") or "").strip()

    # Look for a text/html "body" attachment (no file URL, just rendered HTML).
    for a in (payload.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        if a.get("contentType") == "text/html" and not a.get("contentUrl"):
            html = a.get("content") or ""
            if html:
                rich = _html_to_text(html)
                if len(rich) > len(plain):
                    log.info(
                        "Using HTML body attachment | plain_len=%d | rich_len=%d",
                        len(plain), len(rich),
                    )
                    return rich

    return plain


def _html_to_text(html: str) -> str:
    """Convert Teams message HTML into readable plain text with bullets preserved."""
    s = html.replace("\r\n", "\n").replace("\r", "\n")
    # Drop Skype mention spans so we don't leave 'DefectBot' floating inline.
    s = re.sub(
        r'<span\s+itemtype="http://schema\.skype\.com/Mention"[^>]*>.*?</span>',
        "", s, flags=re.IGNORECASE | re.DOTALL,
    )
    # Bullets
    s = re.sub(r"<li[^>]*>", "  - ", s, flags=re.IGNORECASE)
    s = re.sub(r"</li\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?(ul|ol)[^>]*>", "\n", s, flags=re.IGNORECASE)
    # Paragraphs and line breaks
    s = re.sub(r"</?(p|div)[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    # Strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode the common HTML entities (the rest can stay; ADO HTML field tolerates them).
    s = (s.replace("&amp;", "&")
           .replace("&lt;", "<")
           .replace("&gt;", ">")
           .replace("&quot;", '"')
           .replace("&#39;", "'")
           .replace("&nbsp;", " "))
    # Collapse whitespace, but preserve newlines.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _log_payload_structure(payload: dict) -> None:
    """Temporary diagnostic: dump structure of Teams webhook payload.

    Helps us figure out where Teams stashes attachment metadata in cases
    where payload["attachments"] is empty/missing. Remove once extraction
    is reliable.
    """
    try:
        keys = sorted(payload.keys())
        log.info("payload | top-level keys: %s", keys)

        for field in ("attachments", "entities", "attachmentLayout", "textFormat"):
            if field in payload:
                v = payload[field]
                serialized = v if isinstance(v, str) else json.dumps(v)
                log.info("payload | %s: %s", field, serialized[:2000])

        text = payload.get("text", "") or ""
        if isinstance(text, str):
            log.info("payload | text (first 800 chars): %s", text[:800])

        cd = payload.get("channelData")
        if isinstance(cd, dict):
            log.info("payload | channelData keys: %s", sorted(cd.keys()))
    except Exception:
        log.exception("payload diagnostic failed")


def _extract_attachments(payload: dict) -> list[dict]:
    """Pull attachment metadata from the Teams webhook payload and try to
    download the bytes.

    Returns a list of dicts with keys: {name, url, content}.
      - `content` is bytes if download succeeded, None otherwise.
      - When None, the caller should fall back to embedding the URL as a link.

    Teams typically uses Skype CDN URLs for inline images (auth required) and
    SharePoint URLs for file uploads (also auth, usually). Anonymous GET
    works for some tenant configurations and fails for others — we try
    optimistically and degrade gracefully on 401/403/timeout.
    """
    import requests as _requests

    items = payload.get("attachments") or []
    out: list[dict] = []
    for a in items:
        if not isinstance(a, dict):
            continue
        url = a.get("contentUrl") or ""
        name = a.get("name") or a.get("contentType") or "attachment"
        if not url:
            continue

        content: bytes | None = None
        try:
            r = _requests.get(url, timeout=10)
            if r.ok and r.content:
                content = r.content
                logging.info(
                    "Attachment downloaded | name=%s | bytes=%d", name, len(content)
                )
            else:
                logging.info(
                    "Attachment download non-OK | name=%s | status=%s",
                    name, r.status_code,
                )
        except Exception as e:
            logging.info("Attachment download exception | name=%s | err=%s", name, e)

        out.append({"name": name, "url": url, "content": content})
    return out


def _ack(message: str) -> Response:
    """Return a Teams-compatible JSON message payload."""
    return Response(
        content=json.dumps({"type": "message", "text": message}),
        media_type="application/json",
    )


def _format_success(result: dict) -> str:
    org = os.environ.get("ADO_ORG", "euromonitor")
    project_url = "North%20Star"
    bug_id = result["bugId"]
    bug_url = f"https://dev.azure.com/{org}/{project_url}/_workitems/edit/{bug_id}"

    epic_id = int(os.environ.get("ADO_EPIC_ID", "375255"))
    epic_url = f"https://dev.azure.com/{org}/{project_url}/_workitems/edit/{epic_id}"

    severity = result.get("severity", "")
    category = result.get("category", "")
    summary = result.get("summaryForReporter", "")

    lines = [
        f"Logged **{severity}** {category} bug as "
        f"[#{bug_id}]({bug_url}) under Epic [#{epic_id}]({epic_url})."
    ]

    if result.get("relatedBugId"):
        r_id = result["relatedBugId"]
        r_url = f"https://dev.azure.com/{org}/{project_url}/_workitems/edit/{r_id}"
        lines.append(
            f"**Related to:** [#{r_id}]({r_url}) — "
            f"{result.get('relationReason', 'same symptom')}."
        )

    if result.get("needsMoreInfo"):
        lines.append(
            "_The report was a bit sparse — filed as `[needs-triage]`. "
            "Please reply with more detail when you can._"
        )

    lines.append(f"\n{summary}")
    lines.append("\n_Reply to this message with screenshots if you have any._")

    return "\n\n".join(lines)


# Allow running directly with `python main.py` for quick local checks.
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
