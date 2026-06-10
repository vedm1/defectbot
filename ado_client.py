"""
Azure DevOps REST API wrapper for the UAT Defect Triage Agent.

Three operations the function needs:
  * search_recent_bugs() - fetch up to N recent open Bugs as candidates for dedup.
  * create_bug_with_links() - create a Bug with all fields + Parent link to Epic
    + optional Related link, in a single REST call.
  * health_check() - confirm PAT and connectivity, used by an admin endpoint.
"""
import base64
import logging
import os
import time
from typing import Optional

import requests

ORG = os.environ.get("ADO_ORG", "euromonitor")
PROJECT = os.environ.get("ADO_PROJECT", "North Star")
EPIC_ID = int(os.environ.get("ADO_EPIC_ID", "375255"))

API = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis/wit"
ORG_API = f"https://dev.azure.com/{ORG}/_apis/wit"

# Severity must match the ADO picklist exactly.
ALLOWED_SEVERITIES = {"1 - Critical", "2 - High", "3 - Medium", "4 - Low"}


def _headers() -> dict:
    """Basic auth header using the PAT."""
    pat = os.environ.get("ADO_PAT")
    if not pat:
        raise RuntimeError("ADO_PAT environment variable not set")
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def search_recent_bugs(days: int = 60, top: int = 50) -> list[dict]:
    """
    Return a list of recent open Bug summaries, newest first.

    Each item: {id, title, areaPath, tags, reproExcerpt}.
    """
    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{PROJECT}' "
            "AND [System.WorkItemType] = 'Bug' "
            "AND [System.State] <> 'Closed' "
            "AND [System.State] <> 'Removed' "
            f"AND [System.CreatedDate] >= @Today - {days} "
            "ORDER BY [System.CreatedDate] DESC"
        )
    }
    r = requests.post(
        f"{API}/wiql?api-version=7.1",
        headers={**_headers(), "Content-Type": "application/json"},
        json=wiql,
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("workItems", [])[:top]
    if not items:
        return []

    ids = ",".join(str(w["id"]) for w in items)
    fields = (
        "System.Id,System.Title,System.AreaPath,System.Tags,"
        "Microsoft.VSTS.TCM.ReproSteps"
    )
    r = requests.get(
        f"{API}/workitems?ids={ids}&fields={fields}&api-version=7.1",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()

    out = []
    for w in r.json().get("value", []):
        f = w.get("fields", {})
        repro_raw = f.get("Microsoft.VSTS.TCM.ReproSteps") or ""
        # Strip HTML tags for a clean excerpt that Claude can read.
        repro_text = _strip_html(repro_raw)
        out.append(
            {
                "id": f.get("System.Id"),
                "title": f.get("System.Title", "") or "",
                "areaPath": f.get("System.AreaPath", "") or "",
                "tags": f.get("System.Tags", "") or "",
                "reproExcerpt": repro_text[:300],
            }
        )
    return out


# In-memory TTL cache so we don't WIQL on every form load.
_features_cache: dict = {"ts": 0.0, "data": []}
FEATURES_CACHE_TTL = 300  # seconds


def list_features(force_refresh: bool = False) -> list[dict]:
    """Return all non-Removed Features in the configured project.

    Cached for FEATURES_CACHE_TTL seconds so the /submit page doesn't pay the
    WIQL + batch-fetch cost on every render. Each item is
    {id, title, state, areaPath}.
    """
    now = time.time()
    if not force_refresh and (now - _features_cache["ts"]) < FEATURES_CACHE_TTL:
        return _features_cache["data"]

    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{PROJECT}' "
            "AND [System.WorkItemType] = 'Feature' "
            "AND [System.State] <> 'Removed' "
            "ORDER BY [System.Title]"
        )
    }
    r = requests.post(
        f"{API}/wiql?api-version=7.1",
        headers={**_headers(), "Content-Type": "application/json"},
        json=wiql,
        timeout=10,
    )
    r.raise_for_status()
    items = r.json().get("workItems", [])
    if not items:
        _features_cache.update({"ts": now, "data": []})
        return []

    ids = ",".join(str(w["id"]) for w in items[:200])
    fields = "System.Id,System.Title,System.State,System.AreaPath"
    r = requests.get(
        f"{API}/workitems?ids={ids}&fields={fields}&api-version=7.1",
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()

    out: list[dict] = []
    for w in r.json().get("value", []):
        f = w.get("fields", {})
        fid = f.get("System.Id")
        title = f.get("System.Title", "") or ""
        if fid and title:
            out.append(
                {
                    "id": int(fid),
                    "title": title,
                    "state": f.get("System.State", "") or "",
                    "areaPath": f.get("System.AreaPath", "") or "",
                }
            )

    _features_cache.update({"ts": now, "data": out})
    return out


def upload_attachment(file_name: str, content: bytes) -> str:
    """Upload binary content to ADO's attachments endpoint.

    Returns the attachment URL, which is then used in an "AttachedFile" relation
    on a work item. ADO holds the file blob for as long as some work item
    references it.
    """
    # ADO expects octet-stream POST with the file body. fileName goes in the
    # query string; the API derives the content type from it.
    r = requests.post(
        f"{API}/attachments",
        params={"fileName": file_name, "api-version": "7.1"},
        headers={**_headers(), "Content-Type": "application/octet-stream"},
        data=content,
        timeout=30,
    )
    if not r.ok:
        logging.error(
            "ADO upload_attachment failed: %s %s", r.status_code, r.text[:300]
        )
        r.raise_for_status()
    return r.json()["url"]


def create_bug_with_links(
    title: str,
    repro_html: str,
    severity: str,
    priority: int,
    tags: str,
    area_path: str,
    iteration_path: str,
    related_bug_id: Optional[int] = None,
    related_comment: str = "",
    attached_file_urls: Optional[list[dict]] = None,
    related_feature_id: Optional[int] = None,
) -> int:
    """
    Create a Bug with all fields and links in one request.

    `attached_file_urls` is a list of {url, name} dicts produced by
    upload_attachment(). Each becomes an AttachedFile relation on the Bug,
    which renders inline (for images) in the ADO work item UI.

    Returns the new work item ID.
    """
    if severity not in ALLOWED_SEVERITIES:
        logging.warning(
            "Severity '%s' not in allowed set; defaulting to '3 - Medium'", severity
        )
        severity = "3 - Medium"

    priority = int(priority) if priority in (1, 2, 3, 4) else 3

    patch: list[dict] = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.AreaPath", "value": area_path},
        {"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
        {"op": "add", "path": "/fields/System.Tags", "value": tags},
        {
            "op": "add",
            "path": "/fields/Microsoft.VSTS.TCM.ReproSteps",
            "value": repro_html,
        },
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{ORG_API}/workItems/{EPIC_ID}",
                "attributes": {
                    "comment": "Auto-attached to NSUI MVP Defects Epic by UAT defect agent."
                },
            },
        },
    ]

    if related_bug_id:
        patch.append(
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Related",
                    "url": f"{ORG_API}/workItems/{int(related_bug_id)}",
                    "attributes": {
                        "comment": f"Auto-linked by UAT defect agent: {related_comment}"
                    },
                },
            }
        )

    for att in (attached_file_urls or []):
        patch.append(
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "AttachedFile",
                    "url": att["url"],
                    "attributes": {
                        "comment": f"Uploaded from Teams: {att.get('name', 'attachment')}"
                    },
                },
            }
        )

    if related_feature_id:
        patch.append(
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Related",
                    "url": f"{ORG_API}/workItems/{int(related_feature_id)}",
                    "attributes": {
                        "comment": "Auto-linked to the Feature picked at submission time."
                    },
                },
            }
        )

    r = requests.post(
        f"{API}/workitems/$Bug?api-version=7.1",
        headers={**_headers(), "Content-Type": "application/json-patch+json"},
        json=patch,
        timeout=15,
    )
    if not r.ok:
        logging.error("ADO create_bug failed: %s %s", r.status_code, r.text[:500])
        r.raise_for_status()
    return r.json()["id"]


def health_check() -> dict:
    """Quick sanity check used by the /healthz endpoint."""
    r = requests.get(
        f"{API}/workitems/{EPIC_ID}?api-version=7.1",
        headers=_headers(),
        timeout=5,
    )
    r.raise_for_status()
    return {
        "epicId": EPIC_ID,
        "epicTitle": r.json()["fields"]["System.Title"],
        "project": PROJECT,
        "org": ORG,
    }


def _strip_html(s: str) -> str:
    """Very small HTML-to-text for repro excerpts. Not a full sanitiser."""
    import re

    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</div>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()
