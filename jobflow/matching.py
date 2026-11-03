"""Transparent, deterministic job ranking from user-evidenced skills.

Scores are triage hints, not probabilities. Every inclusion and compensation
claim remains inspectable, and missing information is reported as a gap.
"""

import re


BAY_AREA = ("san francisco", "bay area", "san mateo", "redwood city", "redwood shores",
            "menlo park", "palo alto", "mountain view", "sunnyvale", "santa clara",
            "san jose", "cupertino", "foster city", "san bruno", "south san francisco",
            "oakland", "berkeley", "fremont", "burlingame", "san carlos", "pleasanton",
            "milpitas", "san ramon", "walnut creek", "emeryville")
US_STATES = ("alabama", "alaska", "arizona", "arkansas", "california", "colorado",
             "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
             "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
             "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
             "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
             "new mexico", "new york", "north carolina", "north dakota", "ohio",
             "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
             "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
             "washington", "west virginia", "wisconsin", "wyoming")
US_ABBREVS = "al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc"
US_CITIES = ("seattle", "bellevue", "new york", "austin", "boston", "chicago",
             "los angeles", "san diego", "denver", "portland", "atlanta")
FOREIGN = ("canada", "toronto", "vancouver", "montreal", "united kingdom", "london",
           "ireland", "dublin", "germany", "berlin", "france", "paris", "india",
           "bengaluru", "bangalore", "hyderabad", "china", "singapore", "japan",
           "tokyo", "australia", "sydney", "europe", "emea", "apac", "latam",
           "netherlands", "amsterdam", "switzerland", "zurich", "zürich", "serbia",
           "belgrade", "denmark", "aarhus", "israel", "tel aviv", "brazil", "mexico")
SKILL_ALIASES = {
    "c++": (r"(?<!\w)c\+\+(?!\w)", r"\bcpp\b"),
    "go": (r"\bgolang\b", r"\bgo\s+(?:programming|language|development)\b", r"\b(?:python|java|c\+\+|rust)[, /]+go\b", r"\bgo[, /]+(?:python|java|c\+\+|rust)\b"),
    "golang": (r"\bgolang\b", r"\bgo\s+(?:programming|language)\b", r"\bgo[, /]+(?:python|java|c\+\+|rust)\b"),
    "grpc": (r"\bgrpc\b",),
    "opentelemetry": (r"\bopentelemetry\b", r"\botel\b", r"\bdistributed tracing\b"),
    "otel": (r"\bopentelemetry\b", r"\botel\b", r"\bdistributed tracing\b"),
    "kubernetes": (r"\bkubernetes\b", r"\bk8s\b", r"\bgke\b"),
    "gke": (r"\bgke\b", r"\bgoogle kubernetes engine\b", r"\bkubernetes\b"),
    "api gateways": (r"\bapi gateways?\b", r"\bapi infrastructure\b", r"\bingress\b"),
    "api gateway": (r"\bapi gateways?\b", r"\bapi infrastructure\b", r"\bingress\b"),
    "networking": (r"\bnetwork(?:ing)?\b", r"\btcp(?:/ip)?\b", r"\brdma\b"),
    "distributed systems": (r"\bdistributed (?:systems?|storage|infrastructure|computing)\b",),
    "storage": (r"\bstorage\b", r"\bdatabases?\b"),
    "observability": (r"\bobservability\b", r"\bmonitoring\b", r"\btracing\b"),
    "performance optimization": (r"\bperformance\b", r"\blatency\b", r"\bthroughput\b"),
}


def _has_phrase(text, phrases):
    return any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text) for word in phrases)


def _location_match(job, search):
    location = str(job.get("location") or "").lower().replace("u.s.", "us")
    description = str(job.get("description") or "").lower().replace("u.s.", "us")
    mode = search.get("location_mode", "bay_area_remote")
    if mode in ("any", "anywhere"):
        return True, "Location unrestricted by configuration", None
    bay = _has_phrase(location, BAY_AREA)
    explicit_us = bool(re.search(r"\b(?:united states(?: of america)?|usa|us)\b", location))
    foreign = _has_phrase(location, FOREIGN) or bool(re.search(r"\b(?:uk|eu)\b", location))
    us = explicit_us or bay or (not foreign and (
        _has_phrase(location, US_STATES + US_CITIES) or bool(re.search(r",\s*(?:" + US_ABBREVS + r")\b", location))))
    remote = bool(job.get("remote")) or bool(re.search(r"\bremote\b", location))
    if str(job.get("workplace_type", "")).lower() in ("hybrid", "onsite"):
        remote = False
    if bay:
        return True, "Bay Area location matches preference", None
    # Only explicit location-specific prose can clarify an otherwise unknown
    # remote listing; a boilerplate US benefits paragraph cannot establish this.
    if remote and not location.replace("remote", "").strip(" -/(),"):
        us = bool(re.search(r"remote\s+(?:role\s+)?(?:within|in|across|anywhere in)\s+(?:the\s+)?(?:united states|us)\b", description))
    if remote and us:
        return True, "US remote location matches preference", None
    if mode in ("us", "all_us") and us:
        return True, "US location matches configured search", None
    if mode == "custom":
        preferred = search.get("preferred_locations", [])
        if _has_phrase(location, [str(p).lower() for p in preferred]):
            return True, "Location matches a configured preference", None
    if remote and not us and not foreign:
        return False, "Remote eligibility for US residents is unverified", "Confirm that this remote role permits work from the US"
    if foreign and not us:
        return False, "Location is outside the US search scope", None
    return False, "Location does not match Bay Area or US remote preference", None


def _level(title):
    if re.search(r"\b(?:intern(?:ship)?|new grad(?:uate)?|early career|entry[ -]level|junior|graduate)\b", title):
        return "entry"
    if re.search(r"\b(?:principal|distinguished|fellow)\b", title):
        return "principal"
    if re.search(r"\b(?:director|manager|head of|vp|vice president)\b", title):
        return "management"
    if re.search(r"\bstaff\b", title):
        return "staff"
    if re.search(r"\b(?:senior|sr\.?)\b|\bengineer\s+(?:iii|iv)\b", title):
        return "senior"
    if re.search(r"\b(?:mid[ -]level|intermediate)\b|\bengineer\s+ii\b", title):
        return "mid"
    if re.search(r"\bengineer\s+i\s*(?:$|[-,(])", title):
        return "entry"
    return "unspecified"


def _software_role(title):
    if re.search(r"\b(?:hardware|mechanical|electrical|civil|sales|solutions?|support|field|forward deployed)\s+engineer", title):
        return False
    if re.search(r"\b(?:recruiter|data scientist|product manager|program manager|designer|account executive)\b", title):
        return False
    return bool(re.search(r"\b(?:software|backend|back[ -]end|frontend|front[ -]end|full[ -]?stack|infrastructure|platform|systems?|network(?:ing)?|security|reliability|production|research|machine learning|ml|data|database|storage|compute)\b.*\bengineer|\bengineer.*\b(?:software|backend|infrastructure|platform|systems?|network(?:ing)?|storage|compute)\b|\bmember of technical staff\b|\bsre\b", title))


def _profile_skills(profile):
    skills = profile.get("skills", [])
    if isinstance(skills, dict):
        skills = list(skills)
    result = []
    for skill in skills:
        name = skill.get("name") if isinstance(skill, dict) else skill
        if name and str(name).lower() not in [s.lower() for s in result]:
            result.append(str(name))
    return result


def _match_skill(skill, text):
    patterns = SKILL_ALIASES.get(skill.lower(), (r"(?<!\w)" + re.escape(skill.lower()) + r"(?!\w)",))
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _compensation(job, target):
    comp = job.get("compensation") or {}
    currency = str(comp.get("currency") or "").upper()
    if currency and currency != "USD":
        return "non_usd", 0, "Compensation is quoted in {}; USD target is unverified".format(currency)
    low, high = comp.get("tc_min"), comp.get("tc_max")
    if currency == "USD" and high is not None:
        try:
            high = float(high)
            low = float(low) if low is not None else None
        except (TypeError, ValueError):
            return "unknown", 0, "Total compensation data needs review"
        if low is not None and low >= target:
            return "target_met", 15, "Published total compensation range meets the ${:,.0f} target".format(target)
        if high >= target:
            return "target_possible", 10, "Published total compensation range reaches the ${:,.0f} target".format(target)
        return "below_target", -10, "Published total compensation is below the ${:,.0f} target".format(target)
    if comp.get("base_max") is not None:
        return "unverified_base_only", 0, "Only base salary is published; equity, bonus and total compensation need verification"
    return "unknown", 0, "Total compensation is not published or not verified"


def match_job(job, profile, config):
    """Rank a normalized job and explain hard filters and missing evidence."""
    search = config.get("search", config)
    title = str(job.get("title") or "").lower()
    text = title + "\n" + str(job.get("description") or "").lower()
    reasons, gaps = [], []
    score = 0
    software = _software_role(title)
    if software:
        score += 20
        reasons.append("Software engineering role family")
    else:
        reasons.append("Title does not match the software engineering role scope")
    level = _level(title)
    allowed = search.get("allowed_levels", ["mid", "senior", "staff"])
    level_ok = level in allowed or level == "unspecified"
    employment = str(job.get("employment_type") or "").lower()
    if "intern" in employment:
        level, level_ok = "entry", False
    if level == "unspecified":
        score += 5
        gaps.append("Seniority is not explicit; confirm mid through staff level with the recruiter")
    elif level_ok:
        score += 10
        reasons.append("{} level is within the requested range".format(level.capitalize()))
    else:
        reasons.append("{} level is outside the configured range".format(level.capitalize()))
    location_ok, location_reason, location_gap = _location_match(job, search)
    reasons.append(location_reason)
    if location_ok:
        score += 20
    if location_gap:
        gaps.append(location_gap)
    matched = [skill for skill in _profile_skills(profile) if _match_skill(skill, text)]
    score += min(30, len(matched) * 5)
    if matched:
        reasons.append("Resume-evidenced skill overlap: " + ", ".join(matched))
    else:
        gaps.append("No explicit overlap with the recorded resume skills; review the job description")
    focus = ("distributed systems", "networking", "storage", "infrastructure", "observability", "api gateway", "rpc", "performance", "backend")
    focus_matches = [term for term in focus if _has_phrase(title, [term]) and _has_phrase(text, [term])]
    if focus_matches and matched:
        score += 5
        reasons.append("Role title emphasizes " + ", ".join(focus_matches))
    compensation_status, comp_score, comp_reason = _compensation(job, float(search.get("target_tc", 500000)))
    score += comp_score
    if compensation_status in ("unknown", "unverified_base_only", "non_usd"):
        gaps.append(comp_reason)
    else:
        reasons.append(comp_reason)
    # Missing TC remains a review item, not an invented estimate or rejection.
    min_score = float(search.get("min_score", 35))
    score = int(max(0, min(100, score)))
    eligible = software and level_ok and location_ok and score >= min_score
    if score < min_score:
        reasons.append("Score is below the configured minimum of {}".format(min_score))
    return {"score": score, "eligible": bool(eligible), "reasons": reasons,
            "gaps": gaps, "matched_skills": matched, "compensation_status": compensation_status}
