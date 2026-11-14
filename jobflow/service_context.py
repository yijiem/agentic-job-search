"""Read-only, dated account context; no credentials or live connectors.

Opening a provider or observing a signed-in browser does not establish API
authorization for Jobflow. Only explicitly supported snapshot fields are
projected from imported research records.
"""

import copy
from datetime import date, datetime, timezone
from urllib.parse import urlsplit


_GOOGLE_NOTE = ("Google Sign-In authenticates you to an app; it does not give "
                "Jobflow access to your accounts on other services.")
_GOOGLE_SOURCE = {
    "url": "https://developers.google.com/identity/gsi/web/guides/integrate",
    "title": "Google: authentication and authorization are separate",
}
_SERVICES = (
    {"id": "leetcode", "name": "LeetCode", "url": "https://leetcode.com/",
     "summary": "Use dated coding-progress observations to guide practice.",
     "capability_note": "Browser observations and imported progress are available; "
                        "an official account-data API integration has not been verified. " + _GOOGLE_NOTE,
     "documentation_sources": [{"url": "https://support.leetcode.com/hc/en-us",
                                "title": "LeetCode official help center"}, _GOOGLE_SOURCE]},
    {"id": "teamblind", "name": "Blind", "url": "https://www.teamblind.com/",
     "summary": "Keep dated interview notes and the sources you can access.",
     "capability_note": "A supported account-data connector has not been verified. "
                        "Opening Blind uses your browser session and does not enable background syncing. " + _GOOGLE_NOTE,
     "documentation_sources": [{"url": "https://us.teamblind.com/setting/term_and_privacy",
                                "title": "Blind service and privacy information"}, _GOOGLE_SOURCE]},
    {"id": "1point3acres", "name": "1Point3Acres", "url": "https://www.1point3acres.com/en",
     "summary": "Keep accessible interview reports separate from suggested practice.",
     "capability_note": "A supported account-data connector has not been verified. "
                        "Reports remain subject to your account's access and membership. " + _GOOGLE_NOTE,
     "documentation_sources": [{"url": "https://www.1point3acres.com/en",
                                "title": "1Point3Acres interview preparation"}, _GOOGLE_SOURCE]},
    {"id": "linkedin", "name": "LinkedIn", "url": "https://www.linkedin.com/",
     "summary": "Use your professional profile and exported connection evidence.",
     "capability_note": "LinkedIn sign-in provides limited profile information. Its Connections API "
                        "requires separate approval; your official data export can provide context. " + _GOOGLE_NOTE,
     "documentation_sources": [
         {"url": "https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2",
          "title": "LinkedIn OpenID Connect scopes"},
         {"url": "https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api",
          "title": "LinkedIn Connections API access requirements"},
         {"url": "https://www.linkedin.com/help/linkedin/answer/a1339364/down-your-account-data",
          "title": "Download your LinkedIn data"}, _GOOGLE_SOURCE]},
)
_DOMAINS = {"leetcode": "leetcode.com", "teamblind": "teamblind.com",
            "1point3acres": "1point3acres.com", "linkedin": "linkedin.com"}
_ACCESS = {"signed_in": "Signed-in browser access was observed.",
           "sign_in_required": "The browser showed a sign-in requirement.",
           "public_only": "Only public browser access was observed.",
           "unknown": "Browser sign-in status was not established."}
_METRICS = ("solved_total", "solved_easy", "solved_medium", "solved_hard",
            "total_solved", "easy_solved", "medium_solved", "hard_solved")


def _observed(value):
    """Sort dated observations consistently; never invent a date for an import."""
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _provider_url(value, service):
    """Reject non-provider URLs and URLs that could carry authentication data."""
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        domain = _DOMAINS[service]
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in ("https", "http") or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.port not in (None, 80, 443) or
                not (host == domain or host.endswith("." + domain))):
            return ""
        return value
    except (TypeError, ValueError):
        return ""


def _handle(value):
    if (isinstance(value, str) and 0 < len(value) <= 128 and
            all(c.isalnum() or c in "._-" for c in value)):
        return value
    return ""


def _snapshot(row, service):
    observed = _observed(row.get("observed_at"))
    if observed is None:
        return None
    sources = []
    for source in row.get("sources", []) if isinstance(row.get("sources"), list) else []:
        url = _provider_url(source.get("url"), service) if isinstance(source, dict) else ""
        if url:
            sources.append({"url": url, "title": "Observed " + service + " account page"})
    if not sources:
        return None
    result = {"id": str(row.get("id", ""))[:128], "observed_at": row["observed_at"],
              "sources": sources}
    access = row.get("browser_access", row.get("access_status"))
    if not isinstance(access, str):
        access = None
    if access in _ACCESS:
        result["access_status"] = access
    account = _handle(row.get("account_handle"))
    if account:
        result["account_handle"] = account
    profile_url = _provider_url(row.get("profile_url"), service)
    if profile_url:
        result["profile_url"] = profile_url
    raw_metrics = row.get("metrics", {})
    metrics = {key: raw_metrics[key] for key in _METRICS
               if isinstance(raw_metrics, dict) and type(raw_metrics.get(key)) is int and
               0 <= raw_metrics[key] <= 10 ** 9} if service == "leetcode" else {}
    if metrics:
        result["metrics"] = metrics
    summary = row.get("profile_summary")
    if (service == "linkedin" and isinstance(summary, str) and
            not any(ord(char) < 32 and char not in "\n\t" for char in summary)):
        summary = summary.strip()[:2000]
        if summary:
            result["profile_summary"] = summary
    # Build display text only from the validated projection, not arbitrary
    # research text/metadata that could contain credentials or unrelated claims.
    text = [_ACCESS.get(access, _ACCESS["unknown"])]
    if account:
        text.append("Account: " + account + ".")
    if metrics:
        text.append("Recorded progress: " + "; ".join(
            key.replace("_", " ") + " " + str(value) for key, value in metrics.items()) + ".")
    if result.get("profile_summary"):
        text.append(result["profile_summary"])
    result["text"] = " ".join(text)
    return observed, result


def service_catalog(research_records):
    """Return provider information and safe historical account observations.

    This never establishes a live session, performs network access, modifies
    records, or exposes arbitrary imported metadata. Missing observations mean
    unknown access, not a logged-out or disconnected browser.
    """
    rows = [row for row in research_records if isinstance(row, dict) and row.get("kind") == "service_context"]
    catalog = []
    for definition in _SERVICES:
        service = definition["id"]
        observations = [_snapshot(row, service) for row in rows if row.get("service") == service]
        observations = [item for item in observations if item is not None]
        observations.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        context = [item[1] for item in observations]
        result = {**copy.deepcopy(definition), "mode": "browser_assisted",
                  "connection_status": "not_connected", "context": context,
                  "last_observed_at": context[0]["observed_at"] if context else ""}
        if context:
            for key in ("account_handle", "profile_url", "access_status"):
                if key in context[0]:
                    result[key] = context[0][key]
        catalog.append(result)
    return catalog
