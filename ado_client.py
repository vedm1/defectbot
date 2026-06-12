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

    # ADO's batch GET endpoint caps at 200 IDs per call. Paginate through
    # all items in chunks so we don't silently drop Features past index 200.
    BATCH = 200
    fields = "System.Id,System.Title,System.State,System.AreaPath"
    out: list[dict] = []
    for start in range(0, len(items), BATCH):
        chunk = items[start : start + BATCH]
        ids = ",".join(str(w["id"]) for w in chunk)
        r = requests.get(
            f"{API}/workitems?ids={ids}&fields={fields}&api-version=7.1",
            headers=_headers(),
            timeout=20,
        )
        r.raise_for_status()
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

    # WIQL ordered by title, but batches may have interleaved. Re-sort to keep
    # the dropdown alphabetised.
    out.sort(key=lambda x: x["title"].lower())

    _features_cache.update({"ts": now, "data": out})
    logging.info(
        "list_features cached %d Features (across %d batch GET pages)",
        len(out), (len(items) + BATCH - 1) // BATCH,
    )
    return out


# ----------------------------------------------------------------------------
# Triage board support
# ----------------------------------------------------------------------------

_defects_cache: dict = {"ts": 0.0, "data": []}
DEFECTS_CACHE_TTL = 60  # seconds — short because state changes constantly


def _parse_id_from_url(url: str) -> Optional[int]:
    """Extract the work item ID from a relation URL."""
    try:
        return int(url.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def list_uat_defects(force_refresh: bool = False) -> list[dict]:
    """Fetch all Bugs tagged uat-defect with their relations expanded.

    Returns each as a dict with:
      id, title, state, severity, priority, areaPath, tags (list), clusterTag,
      createdDate, createdBy (display name), createdByEmail,
      featureId (or None), featureTitle (or None),
      relatedBugs ([{id, title}, ...]),
      duplicateOf (id or None),
      duplicates ([{id}, ...]).

    Cached for DEFECTS_CACHE_TTL seconds. Set force_refresh=True after a
    state-change action to bust the cache.
    """
    now = time.time()
    if not force_refresh and (now - _defects_cache["ts"]) < DEFECTS_CACHE_TTL:
        return _defects_cache["data"]

    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{PROJECT}' "
            "AND [System.WorkItemType] = 'Bug' "
            "AND [System.Tags] CONTAINS 'uat-defect' "
            "ORDER BY [System.CreatedDate] DESC"
        )
    }
    r = requests.post(
        f"{API}/wiql?api-version=7.1",
        headers={**_headers(), "Content-Type": "application/json"},
        json=wiql,
        timeout=15,
    )
    r.raise_for_status()
    items = r.json().get("workItems", [])
    if not items:
        _defects_cache.update({"ts": now, "data": []})
        return []

    # Fetch bugs with relations expanded so we see Feature + Related + Duplicate.
    BATCH = 200
    bugs_raw: list[dict] = []
    for start in range(0, len(items), BATCH):
        chunk = items[start : start + BATCH]
        ids = ",".join(str(w["id"]) for w in chunk)
        r = requests.get(
            f"{API}/workitems?ids={ids}&$expand=relations&api-version=7.1",
            headers=_headers(),
            timeout=25,
        )
        r.raise_for_status()
        bugs_raw.extend(r.json().get("value", []))

    # Collect ALL related work-item IDs across all bugs so we can fetch their
    # types + titles in one (or two) batch GET(s). Otherwise we can't tell
    # whether a "Related" link points at a Feature or another Bug.
    referenced_ids: set[int] = set()
    for w in bugs_raw:
        for rel in w.get("relations") or []:
            rtype = rel.get("rel") or ""
            if rtype in (
                "System.LinkTypes.Related",
                "System.LinkTypes.Duplicate-Forward",
                "System.LinkTypes.Duplicate-Reverse",
            ):
                rid = _parse_id_from_url(rel.get("url") or "")
                if rid:
                    referenced_ids.add(rid)

    # Look up titles + types for referenced items.
    ref_info: dict[int, dict] = {}
    if referenced_ids:
        ids_list = sorted(referenced_ids)
        for start in range(0, len(ids_list), BATCH):
            chunk = ids_list[start : start + BATCH]
            ids = ",".join(str(i) for i in chunk)
            r = requests.get(
                f"{API}/workitems?ids={ids}"
                "&fields=System.Id,System.Title,System.WorkItemType,System.State"
                "&api-version=7.1",
                headers=_headers(),
                timeout=20,
            )
            r.raise_for_status()
            for w in r.json().get("value", []):
                f = w.get("fields", {})
                wid = f.get("System.Id")
                if wid:
                    ref_info[int(wid)] = {
                        "id": int(wid),
                        "title": f.get("System.Title", "") or "",
                        "workItemType": f.get("System.WorkItemType", "") or "",
                        "state": f.get("System.State", "") or "",
                    }

    # Assemble the final list of defects with denormalised relations.
    out: list[dict] = []
    for w in bugs_raw:
        f = w.get("fields", {})
        tags_raw = (f.get("System.Tags") or "").strip()
        tags = [t.strip() for t in tags_raw.split(";") if t.strip()]
        cluster_tag = next(
            (t for t in tags if t.lower().startswith("uat-cluster-")), ""
        )

        feature_id: Optional[int] = None
        feature_title = ""
        related_bugs: list[dict] = []
        duplicate_of: Optional[int] = None
        duplicates: list[dict] = []

        for rel in (w.get("relations") or []):
            rtype = rel.get("rel") or ""
            rid = _parse_id_from_url(rel.get("url") or "")
            if not rid or rid not in ref_info:
                continue
            info = ref_info[rid]
            if rtype == "System.LinkTypes.Related":
                if info["workItemType"] == "Feature":
                    feature_id = rid
                    feature_title = info["title"]
                elif info["workItemType"] == "Bug":
                    related_bugs.append({"id": rid, "title": info["title"], "state": info["state"]})
            elif rtype == "System.LinkTypes.Duplicate-Forward":
                # This bug is a duplicate of `rid`.
                duplicate_of = rid
            elif rtype == "System.LinkTypes.Duplicate-Reverse":
                # `rid` is a duplicate of this bug.
                duplicates.append({"id": rid, "title": info["title"]})

        created_by = f.get("System.CreatedBy") or {}
        out.append(
            {
                "id": int(f.get("System.Id")),
                "title": f.get("System.Title", "") or "",
                "state": f.get("System.State", "") or "",
                "severity": f.get("Microsoft.VSTS.Common.Severity", "") or "",
                "priority": f.get("Microsoft.VSTS.Common.Priority", 3) or 3,
                "areaPath": f.get("System.AreaPath", "") or "",
                "tags": tags,
                "clusterTag": cluster_tag,
                "createdDate": f.get("System.CreatedDate", "") or "",
                "createdBy": (created_by.get("displayName") or "Unknown")
                if isinstance(created_by, dict) else "Unknown",
                "createdByEmail": (created_by.get("uniqueName") or "")
                if isinstance(created_by, dict) else "",
                "featureId": feature_id,
                "featureTitle": feature_title,
                "relatedBugs": related_bugs,
                "duplicateOf": duplicate_of,
                "duplicates": duplicates,
            }
        )

    _defects_cache.update({"ts": now, "data": out})
    logging.info(
        "list_uat_defects cached %d defects (%d referenced items resolved)",
        len(out), len(ref_info),
    )
    return out


def bulk_update_state(bug_ids: list[int], new_state: str) -> list[dict]:
    """Set System.State on a list of Bug work items.

    Returns one dict per bug with {id, ok, error}. The cache is invalidated
    after at least one successful update.
    """
    results: list[dict] = []
    patch = [{"op": "add", "path": "/fields/System.State", "value": new_state}]
    headers = {**_headers(), "Content-Type": "application/json-patch+json"}
    any_ok = False
    for bug_id in bug_ids:
        try:
            r = requests.patch(
                f"{API}/workitems/{int(bug_id)}?api-version=7.1",
                headers=headers, json=patch, timeout=15,
            )
            if r.ok:
                results.append({"id": bug_id, "ok": True, "error": ""})
                any_ok = True
            else:
                results.append(
                    {"id": bug_id, "ok": False, "error": f"{r.status_code} {r.text[:200]}"}
                )
                logging.warning(
                    "bulk_update_state failed | bug=%s | status=%s | body=%s",
                    bug_id, r.status_code, r.text[:200],
                )
        except Exception as e:
            results.append({"id": bug_id, "ok": False, "error": str(e)[:200]})
            logging.exception("bulk_update_state exception | bug=%s", bug_id)
    if any_ok:
        _defects_cache["ts"] = 0.0  # bust cache
    return results


def mark_as_duplicates(
    canonical_id: int, duplicate_ids: list[int], close_state: str = "Done"
) -> list[dict]:
    """For each ID in duplicate_ids, add a Duplicate-Forward link pointing to
    canonical_id and (optionally) move the bug to close_state in one PATCH.

    Returns one dict per duplicate with {id, ok, error}.
    """
    results: list[dict] = []
    headers = {**_headers(), "Content-Type": "application/json-patch+json"}
    any_ok = False
    for dup_id in duplicate_ids:
        if int(dup_id) == int(canonical_id):
            results.append({"id": dup_id, "ok": False, "error": "Cannot mark canonical as duplicate of itself"})
            continue
        patch = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Duplicate-Forward",
                    "url": f"{ORG_API}/workItems/{int(canonical_id)}",
                    "attributes": {
                        "comment": f"Marked as duplicate of #{int(canonical_id)} via triage UI."
                    },
                },
            }
        ]
        if close_state:
            patch.append(
                {"op": "add", "path": "/fields/System.State", "value": close_state}
            )
        try:
            r = requests.patch(
                f"{API}/workitems/{int(dup_id)}?api-version=7.1",
                headers=headers, json=patch, timeout=15,
            )
            if r.ok:
                results.append({"id": dup_id, "ok": True, "error": ""})
                any_ok = True
            else:
                results.append(
                    {"id": dup_id, "ok": False, "error": f"{r.status_code} {r.text[:200]}"}
                )
                logging.warning(
                    "mark_as_duplicates failed | dup=%s | canonical=%s | status=%s | body=%s",
                    dup_id, canonical_id, r.status_code, r.text[:200],
                )
        except Exception as e:
            results.append({"id": dup_id, "ok": False, "error": str(e)[:200]})
            logging.exception("mark_as_duplicates exception | dup=%s", dup_id)
    if any_ok:
        _defects_cache["ts"] = 0.0  # bust cache
    return results


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
