"""
Claude integration for the UAT Defect Triage Agent.

Single-shot Anthropic API call: feed the new defect (free-text) and a list of
recent open Bugs, get back a strict JSON object with cleaned-up fields and
optional relatedBugId. Then create the bug in ADO and return a summary dict.
"""
import json
import logging
import os
from typing import Any

from anthropic import Anthropic

from ado_client import create_bug_with_links, search_recent_bugs, upload_attachment

_client: Anthropic | None = None


def _claude() -> Anthropic:
    """Lazy Anthropic client (so import works even without a key set yet)."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")
        _client = Anthropic(api_key=api_key)
    return _client


MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ITERATION_PATH = os.environ.get("ADO_ITERATION_PATH", "North Star\\PI4")


SYSTEM_PROMPT = """You are the UAT Defect Triage Agent for the North Star MVP project at Euromonitor.

You receive a user's natural-language defect report posted in a Microsoft Teams channel as an @mention to the bot. You must:
  1. Extract structured fields from the free-text.
  2. Decide whether it duplicates / is related to any existing open Bug from the candidates list.
  3. Return a strict JSON object that the Azure Function uses to create a Bug in Azure DevOps.

OUTPUT CONTRACT
Return a single JSON object — no markdown fences, no commentary before or after:

{
  "cleanTitle": string,           // <=120 chars, imperative voice
  "reproStepsHtml": string,       // HTML <div> blocks: Steps / Expected / Actual / Environment / Browser / Screenshot
  "severity": "1 - Critical" | "2 - High" | "3 - Medium" | "4 - Low",
  "priority": 1 | 2 | 3 | 4,      // 1:Critical, 2:High/Medium, 3:Medium-Low, 4:Low
  "category": string,             // e.g. "Export/Performance", "Auth", "Data", "UI"
  "clusterTag": string,           // kebab-case, prefixed "uat-cluster-"
  "tags": string,                 // semicolon-separated; ALWAYS includes "uat-defect" plus the clusterTag
  "areaPath": string,             // valid path under "North Star"; default "North Star"
  "relatedBugId": number | null,  // ADO id from the candidates list, or null
  "confidence": number,           // 0.0-1.0
  "relationReason": string,       // 1 sentence why related; "" if no related bug
  "summaryForReporter": string,   // <=200 chars, friendly Teams reply text
  "needsMoreInfo": boolean        // true if report is too sparse to file cleanly
}

RULES
1. Tags ALWAYS includes "uat-defect" and clusterTag.
2. Severity uses the exact strings above (number + " - " + label).
3. relatedBugId must come from the provided candidates list — never invent.
4. Confidence thresholds:
   - >= 0.85: same underlying issue
   - 0.60-0.84: probably related
   - <  0.60: weakly related — set relatedBugId to null
5. Clean the title: no ALL CAPS, no emoji, drop "BUG:" / "Defect:" prefixes.
6. reproStepsHtml uses only <div>, <br>, <b>, <ul>, <li>, <a>. Never <script> or inline JS.
7. If a screenshot URL is present, include it as a clickable <a> at the bottom of reproStepsHtml.
8. If the report is too sparse (no clear symptom or steps), set needsMoreInfo=true,
   prefix cleanTitle with "[needs-triage] ", set severity to "4 - Low".
9. Do NOT add the [agent-test] prefix.
"""


def triage_defect(
    user_text: str,
    reporter_name: str,
    reporter_email: str,
    attachments: list[dict] | None = None,
    extra_tags: list[str] | None = None,
    feature_id: int | None = None,
) -> dict[str, Any]:
    """
    End-to-end triage. Returns a summary dict used by the Teams reply formatter.

    `attachments` is a list of {name, url, content} dicts. Bytes get uploaded
    to ADO as AttachedFile relations; URL-only items are linked in Repro Steps.

    `extra_tags` is an optional list of explicit tags from the submission form
    (e.g. ['Bug_AlphaUAT1']). These get merged with Claude's auto-tags
    (`uat-defect`, the cluster tag) before the Bug is created. Case-insensitive
    de-dupe so we never write the same tag twice.

    Side effect: creates a Bug in ADO with all required links.
    """
    attachments = attachments or []
    extra_tags = extra_tags or []
    candidates = search_recent_bugs(days=60, top=50)
    candidates_block = "\n".join(
        f"[#{c['id']}] {c['title']} | Area: {c['areaPath']} | "
        f"Tags: {c['tags']} | Repro: {c['reproExcerpt']}"
        for c in candidates
    ) or "(no recent open bugs)"

    user_message = (
        "=== NEW DEFECT REPORT ===\n"
        f"Reporter: {reporter_name} <{reporter_email}>\n"
        "Free-text submission:\n\n"
        f"{user_text}\n\n"
        "=== RECENT OPEN BUGS IN NORTH STAR (last 60 days) ===\n"
        f"{candidates_block}\n\n"
        "=== END ===\n"
        "Return the JSON object now. No prose, no markdown fences, no commentary."
    )

    logging.info(
        "Calling Claude | model=%s | candidates=%d | input_chars=%d",
        MODEL, len(candidates), len(user_message),
    )

    response = _claude().messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()

    triage = _safe_parse_json(raw)
    triage = _normalise(triage)

    # Merge in any explicit form-selected tags. ADO uses "; " as the separator.
    if extra_tags:
        existing_lower = {
            t.strip().lower()
            for t in (triage.get("tags") or "").split(";")
            if t.strip()
        }
        additions = [t for t in extra_tags if t.strip().lower() not in existing_lower]
        if additions:
            current = (triage.get("tags") or "").strip()
            triage["tags"] = (
                current + "; " + "; ".join(additions) if current else "; ".join(additions)
            )

    # Handle Teams attachments: prefer uploading to ADO; fall back to URL
    # passthrough for any whose bytes we couldn't fetch (auth-protected URLs).
    uploaded: list[dict] = []
    fallback_links: list[dict] = []
    for att in attachments:
        if att.get("content"):
            try:
                ado_url = upload_attachment(att["name"], att["content"])
                uploaded.append({"url": ado_url, "name": att["name"]})
                logging.info("Attachment uploaded to ADO | name=%s", att["name"])
            except Exception:
                logging.exception(
                    "ADO attachment upload failed | name=%s", att.get("name")
                )
                fallback_links.append(att)
        else:
            fallback_links.append(att)

    repro_html = triage["reproStepsHtml"]
    if fallback_links:
        items_html = "".join(
            f'<li><a href="{a["url"]}">{a["name"]}</a></li>' for a in fallback_links
        )
        repro_html += (
            "<div><br></div><div><b>Attachments (Teams link only — could not download):</b>"
            f"<ul>{items_html}</ul>"
            "<i>Sign-in to Teams / SharePoint may be required.</i></div>"
        )

    bug_id = create_bug_with_links(
        title=triage["cleanTitle"],
        repro_html=repro_html,
        severity=triage["severity"],
        priority=int(triage["priority"]),
        tags=triage["tags"],
        area_path=triage.get("areaPath") or "North Star",
        iteration_path=ITERATION_PATH,
        related_bug_id=triage.get("relatedBugId"),
        related_comment=triage.get("relationReason", ""),
        attached_file_urls=uploaded,
        related_feature_id=feature_id,
    )

    return {
        "bugId": bug_id,
        "severity": triage["severity"],
        "category": triage.get("category", "Unclassified"),
        "relatedBugId": triage.get("relatedBugId"),
        "relationReason": triage.get("relationReason", ""),
        "summaryForReporter": triage.get("summaryForReporter", ""),
        "needsMoreInfo": triage.get("needsMoreInfo", False),
    }


def _safe_parse_json(raw: str) -> dict:
    """Parse Claude's response, stripping any accidental markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop opening fence (with or without language tag) and closing fence.
        text = text.split("```", 2)[1]
        if text.lower().startswith("json"):
            text = text[4:]
        if "```" in text:
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    return json.loads(text)


def _normalise(t: dict) -> dict:
    """Defensive cleanup before we trust the JSON."""
    t = dict(t)

    # Force the iteration constant — Claude must not deviate.
    t["iterationPath"] = ITERATION_PATH

    # Tags always include uat-defect.
    tags = (t.get("tags") or "").strip()
    if "uat-defect" not in tags.lower():
        tags = "uat-defect" + ("; " + tags if tags else "")
    cluster = (t.get("clusterTag") or "").strip()
    if cluster and cluster.lower() not in tags.lower():
        tags = f"{tags}; {cluster}"
    t["tags"] = tags

    # Severity must be in allowed set; default to Medium.
    allowed = {"1 - Critical", "2 - High", "3 - Medium", "4 - Low"}
    if t.get("severity") not in allowed:
        t["severity"] = "3 - Medium"

    # Priority is an int 1-4.
    try:
        p = int(t.get("priority", 3))
        t["priority"] = p if p in (1, 2, 3, 4) else 3
    except (TypeError, ValueError):
        t["priority"] = 3

    # relatedBugId is either int or None.
    rb = t.get("relatedBugId")
    try:
        t["relatedBugId"] = int(rb) if rb else None
    except (TypeError, ValueError):
        t["relatedBugId"] = None

    return t
