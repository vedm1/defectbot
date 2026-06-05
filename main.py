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

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per uploaded screenshot
MAX_FILES = 6

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
# Web form (alternative submission UI alongside the @DefectBot Teams flow)
# ----------------------------------------------------------------------------
_FORM_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       background: #f3f3f6; color: #1e1e1e; margin: 0; padding: 40px 16px; }
.wrap { max-width: 720px; margin: 0 auto; }
.card { background: #fff; padding: 32px; border-radius: 12px;
        box-shadow: 0 1px 3px rgba(0,0,0,.06), 0 8px 24px rgba(0,0,0,.04); }
h1 { margin: 0 0 8px; font-size: 22px; font-weight: 600; }
.sub { color: #555; margin: 0 0 28px; font-size: 14px; }
label { display: block; margin-top: 18px; font-size: 13px; font-weight: 600;
        color: #333; }
.hint { font-weight: 400; color: #777; font-size: 12px; }
input[type=text], input[type=email], input[type=url], textarea, select {
        width: 100%; padding: 9px 11px; margin-top: 6px; font-size: 14px;
        border: 1px solid #d4d4d8; border-radius: 6px; background: #fff;
        font-family: inherit; }
textarea { min-height: 88px; resize: vertical; }
input:focus, textarea:focus, select:focus { outline: 2px solid #6264a7;
        outline-offset: -1px; border-color: #6264a7; }
button { margin-top: 28px; padding: 11px 28px; background: #6264a7;
        color: #fff; border: 0; border-radius: 6px; font-size: 14px;
        font-weight: 600; cursor: pointer; }
button:hover { background: #5258a0; }
button:disabled { background: #aaa; cursor: not-allowed; }
.banner { padding: 16px 20px; border-radius: 8px; margin-bottom: 24px;
        font-size: 14px; line-height: 1.5; }
.banner.ok { background: #ecfdf3; border: 1px solid #b2efc2; color: #15532b; }
.banner.err { background: #fef2f2; border: 1px solid #fecaca; color: #7b1d1d; }
.banner a { color: inherit; font-weight: 600; }
.row { display: flex; gap: 16px; }
.row > label { flex: 1; }
"""


def _form_html(banner: str = "") -> str:
    """Render the defect submission form. `banner` is optional pre-rendered HTML."""
    return f"""<!doctype html><html lang=\"en\"><head>
<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Report a UAT Defect</title><style>{_FORM_CSS}</style></head>
<body><div class=\"wrap\"><div class=\"card\">
{banner}
<h1>Report a UAT Defect</h1>
<p class=\"sub\">The agent triages your report against open Bugs in <b>North Star</b>,
classifies it, and files under Epic #375255 — NSUI | MVP Defects (Iteration <code>North Star\\PI4</code>).
A copy of the result also lands in the UAT Defects Teams channel.</p>

<form method=\"post\" action=\"/submit\" enctype=\"multipart/form-data\">
  <div class=\"row\">
    <label>Reporter name
      <input type=\"text\" name=\"reporter_name\" required placeholder=\"Your name\">
    </label>
    <label>Reporter email <span class=\"hint\">(optional)</span>
      <input type=\"email\" name=\"reporter_email\" placeholder=\"you@euromonitor.com\">
    </label>
  </div>

  <label>Title
    <input type=\"text\" name=\"title\" required maxlength=\"160\"
           placeholder=\"Short summary, e.g. Excel export fails on Market Sizes for large filters\">
  </label>

  <label>Steps to reproduce
    <textarea name=\"steps\" required placeholder=\"1. ...\n2. ...\n3. ...\"></textarea>
  </label>

  <div class=\"row\">
    <label>Expected
      <textarea name=\"expected\" required placeholder=\"What should have happened\"></textarea>
    </label>
    <label>Actual
      <textarea name=\"actual\" required placeholder=\"What happened, including errors\"></textarea>
    </label>
  </div>

  <div class=\"row\">
    <label>Severity
      <select name=\"severity\">
        <option value=\"1 - Critical\">1 - Critical</option>
        <option value=\"2 - High\">2 - High</option>
        <option value=\"3 - Medium\" selected>3 - Medium</option>
        <option value=\"4 - Low\">4 - Low</option>
      </select>
    </label>
    <label>Module
      <select name=\"module\">
        <option>NSUI</option>
        <option>Hydra Platform</option>
        <option>Hydra Ingestion</option>
        <option>Auth</option>
        <option>Other</option>
      </select>
    </label>
  </div>

  <label>Environment <span class=\"hint\">(browser + OS)</span>
    <input type=\"text\" name=\"environment\" placeholder=\"Chrome 124 on Windows 11\">
  </label>

  <label>Screenshots <span class=\"hint\">(PNG / JPG, up to {MAX_FILE_BYTES // (1024 * 1024)}MB each, max {MAX_FILES} files)</span>
    <input type=\"file\" name=\"screenshots\" multiple accept=\"image/*\">
  </label>

  <label>Screenshot link <span class=\"hint\">(OneDrive or SharePoint share URL, view-only — optional, in addition to or instead of direct upload)</span>
    <input type=\"url\" name=\"screenshot_url\" placeholder=\"https://euromonitor.sharepoint.com/...\">
  </label>

  <button type=\"submit\">Submit defect</button>
</form>
</div></div></body></html>"""


@app.get("/submit", response_class=HTMLResponse)
async def submit_form() -> HTMLResponse:
    """Serve the defect submission form."""
    return HTMLResponse(_form_html())


@app.post("/submit", response_class=HTMLResponse)
async def submit_handle(
    reporter_name: str = Form(...),
    reporter_email: str = Form(""),
    title: str = Form(...),
    steps: str = Form(...),
    expected: str = Form(...),
    actual: str = Form(...),
    severity: str = Form("3 - Medium"),
    module: str = Form(""),
    environment: str = Form(""),
    screenshot_url: str = Form(""),
    screenshots: list[UploadFile] = File(default_factory=list),
) -> HTMLResponse:
    """Handle the form POST. Run triage, post to Teams, re-render with a result banner."""
    # 1. Compose a single text string for Claude — same shape as a
    #    structured @DefectBot message.
    text_parts = [f"Title: {title}", "", "Steps to reproduce:", steps.strip(), ""]
    text_parts += [f"Expected: {expected.strip()}", f"Actual: {actual.strip()}", ""]
    text_parts += [
        f"Severity: {severity}",
        f"Module: {module}" if module else "",
        f"Environment: {environment}" if environment else "",
        f"Screenshot: {screenshot_url}" if screenshot_url else "",
    ]
    text = "\n".join(p for p in text_parts if p is not None).strip()

    # 2. Read uploaded screenshots into the {name, url, content} shape
    #    triage_defect already understands.
    attachments: list[dict] = []
    skipped: list[str] = []
    for upload in (screenshots or [])[:MAX_FILES]:
        if not upload or not upload.filename:
            continue
        try:
            content = await upload.read()
        except Exception:
            log.exception("Could not read uploaded file | name=%s", upload.filename)
            skipped.append(f"{upload.filename} (read error)")
            continue
        if not content:
            continue
        if len(content) > MAX_FILE_BYTES:
            log.warning(
                "Skipping oversized attachment | name=%s | bytes=%d",
                upload.filename, len(content),
            )
            skipped.append(f"{upload.filename} (>{MAX_FILE_BYTES // (1024*1024)}MB)")
            continue
        attachments.append(
            {"name": upload.filename, "url": "", "content": content}
        )

    log.info(
        "form-submit start | reporter=%s | text_len=%d | attachments=%d",
        reporter_name, len(text), len(attachments),
    )

    try:
        result = triage_defect(
            user_text=text,
            reporter_name=reporter_name,
            reporter_email=reporter_email,
            attachments=attachments,
        )
        log.info(
            "form-submit success | bug=%s | related=%s | attached=%d",
            result.get("bugId"), result.get("relatedBugId"), len(attachments),
        )
        # Best-effort Teams notification (same as Outgoing Webhook path)
        try:
            post_to_channel(_format_success(result))
        except Exception:
            log.exception("Form result: failed to post to Teams (continuing)")

        banner = _success_banner_html(result, len(attachments), skipped)
    except Exception as e:
        log.exception("form-submit failed")
        banner = (
            f'<div class="banner err"><b>Submission failed.</b> '
            f'{_escape(str(e))[:300]}</div>'
        )

    return HTMLResponse(_form_html(banner))


def _success_banner_html(
    result: dict,
    attached_count: int = 0,
    skipped: list[str] | None = None,
) -> str:
    org = os.environ.get("ADO_ORG", "euromonitor")
    project = "North%20Star"
    bug_id = result.get("bugId")
    bug_url = f"https://dev.azure.com/{org}/{project}/_workitems/edit/{bug_id}"
    severity = _escape(result.get("severity", ""))
    category = _escape(result.get("category", ""))

    related_html = ""
    if result.get("relatedBugId"):
        r_id = result["relatedBugId"]
        r_url = f"https://dev.azure.com/{org}/{project}/_workitems/edit/{r_id}"
        related_html = (
            f' &middot; <b>Related to:</b> <a href="{r_url}" target="_blank">#{r_id}</a>'
        )

    attach_html = ""
    if attached_count > 0:
        plural = "s" if attached_count != 1 else ""
        attach_html = f' &middot; <b>{attached_count} screenshot{plural} attached</b>'

    skipped_html = ""
    if skipped:
        items = ", ".join(_escape(s) for s in skipped)
        skipped_html = (
            f'<div style="margin-top:6px;font-size:12px;color:#7b5e1d">'
            f'Skipped: {items}</div>'
        )

    return (
        f'<div class="banner ok"><b>Logged {severity} {category} bug:</b> '
        f'<a href="{bug_url}" target="_blank">#{bug_id}</a>'
        f'{related_html}{attach_html}{skipped_html}</div>'
    )


def _escape(s: str) -> str:
    """Minimal HTML-escape for safe-by-default rendering of user-supplied text."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


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
