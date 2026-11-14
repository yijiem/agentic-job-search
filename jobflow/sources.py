"""Read-only public ATS connectors, normalization and stable job identities.

No login cookies, application endpoints, or private APIs are used. Connector
errors are deliberately surfaced so an unavailable board is not an empty board.
"""

import hashlib
import html
from html.parser import HTMLParser
import json
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class SourceError(RuntimeError):
    """A source could not be read or returned an unexpected schema."""


TRACKING_KEYS = {"gh_src", "lever-source", "lever-origin", "fbclid", "gclid",
                 "mc_cid", "mc_eid", "trk", "trackingid"}
# Large public employer boards can exceed 32 MiB with full descriptions.
# Keep a hard cap rather than trusting Content-Length or reading unbounded data.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def canonical_url(url):
    """Remove known tracking parameters while preserving job identity queries."""
    parts = urlsplit(str(url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def job_id(job):
    """Stable source-scoped identity; canonical URLs enable cross-source dedup."""
    company = str(job.get("company", "")).strip().casefold()
    external_id = job.get("external_id")
    if external_id is not None and str(external_id).strip():
        identity = "id:" + str(job.get("source_id") or "").strip() + ":" + str(external_id).strip()
    elif job.get("url"):
        identity = "url:" + canonical_url(job["url"])
    else:
        raise ValueError("A job needs an external_id or URL")
    return hashlib.sha256((company + "\n" + identity).encode("utf-8")).hexdigest()[:24]


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    text = str(value or "")
    # Greenhouse may HTML-escape the entire document, sometimes twice.
    for _ in range(2):
        text = html.unescape(text)
    parser = _TextParser()
    parser.feed(text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip()
                     for line in "".join(parser.parts).splitlines() if line.strip())


def _request_json(url, timeout=20, retries=2):
    request = Request(url, headers={"Accept": "application/json",
                                   "User-Agent": "Jobflow/0.1 (public job-board reader)"})
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SourceError("Job board response exceeds the {:g} MiB limit".format(
                    MAX_RESPONSE_BYTES / (1024 * 1024)))
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise SourceError("Job board returned invalid JSON") from exc
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise SourceError("HTTP {} from {}".format(exc.code, url)) from exc
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            delay = min(5.0, float(retry_after)) if retry_after.isdigit() else min(5, 2 ** attempt)
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise SourceError("Unable to fetch {}: {}".format(url, exc)) from exc
            delay = min(5, 2 ** attempt)
        time.sleep(delay)
    raise SourceError("Unable to fetch job board")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if 0 <= value < 1000000000 else None
    except (TypeError, ValueError):
        return None


_MONEY_RANGE = re.compile(
    r"(?P<prefix>US\$|USD\s*\$?|\$|CAD\s*\$?|C\$|GBP\s*|£|EUR\s*|€)\s*"
    r"(?P<low>\d[\d,]*(?:\.\d+)?)\s*(?P<low_k>[kK]?)\s*"
    r"(?:[-–—]|\bto\b)\s*(?:US\$|USD\s*\$?|\$|CAD\s*\$?|C\$|£|€)?\s*"
    r"(?P<high>\d[\d,]*(?:\.\d+)?)\s*(?P<high_k>[kK]?)"
)


def compensation_from_text(description):
    """Extract only explicit salary/TC ranges; never estimate equity or bonus."""
    result = {"raw": "", "base_min": None, "base_max": None, "currency": None,
              "tc_min": None, "tc_max": None, "tc_status": "unknown"}
    found = {"base": [], "tc": []}
    snippets = []
    for match in _MONEY_RANGE.finditer(description):
        # A close explicit label is required. Generic 'compensation' may mean
        # base only and is intentionally not interpreted as total compensation.
        before = description[max(0, match.start() - 180):match.start()]
        after = description[match.end():match.end() + 65]
        context = before + match.group(0) + after
        labels = list(re.finditer(r"total\s+(?:annual\s+)?compensation|base\s+(?:annual\s+)?(?:salary|pay)|annual\s+salary|salary\s+range", before, re.I))
        label = labels[-1].group(0).lower() if labels else ""
        if not label:
            following = re.match(r"\s*(?:USD|per year|annually)?[\s,;()]*\s*(base salary|base pay|total compensation)\b", after, re.I)
            label = following.group(1).lower() if following else ""
        kind = "tc" if "total" in label else "base" if label else None
        if kind is None:
            continue
        low = float(match.group("low").replace(",", ""))
        high = float(match.group("high").replace(",", ""))
        if match.group("low_k"):
            low *= 1000
        if match.group("high_k"):
            high *= 1000
            if not match.group("low_k") and low < 1000:
                low *= 1000
        # Do not mistake hourly pay, equity percentages, or malformed ranges
        # for annual compensation.
        if low > high or low < 10000 or re.match(r"\s*(?:/\s*(?:hr|hour)|per hour)", after, re.I):
            continue
        prefix = match.group("prefix").upper()
        currency = "CAD" if prefix.startswith(("CAD", "C$")) else "GBP" if "£" in prefix or prefix == "GBP" else "EUR" if "€" in prefix or prefix == "EUR" else "USD"
        if re.match(r"\s*CAD\b", after, re.I):
            currency = "CAD"
        found[kind].append((low, high, currency))
        snippets.append(context.strip())
    currencies = {entry[2] for values in found.values() for entry in values}
    # Do not blend incomparable currencies into one numeric range.
    if len(currencies) == 1:
        result["currency"] = currencies.pop()
        for kind, values in found.items():
            if values:
                result[kind + "_min"] = min(v[0] for v in values)
                result[kind + "_max"] = max(v[1] for v in values)
    result["raw"] = "\n".join(snippets)[:4000]
    result["tc_status"] = "published_total_compensation" if result["tc_max"] is not None else "base_only" if result["base_max"] is not None else "unknown"
    return result


def _ashby_compensation(record, description):
    result = compensation_from_text(description)
    comp = record.get("compensation")
    if not isinstance(comp, dict):
        return result
    components = comp.get("summaryComponents", [])
    if not isinstance(components, list) or not components:
        components = [component for tier in comp.get("compensationTiers", [])
                      if isinstance(tier, dict) for component in tier.get("components", [])]
    salaries = [c for c in components if isinstance(c, dict)
                and c.get("compensationType") == "Salary"
                and str(c.get("interval", "")).upper() in ("1 YEAR", "YEAR", "ANNUAL")
                and _number(c.get("minValue")) is not None and _number(c.get("maxValue")) is not None
                and _number(c["minValue"]) <= _number(c["maxValue"])]
    currencies = {c.get("currencyCode") for c in salaries}
    if salaries and len(currencies) == 1 and next(iter(currencies)):
        result.update(base_min=min(float(c["minValue"]) for c in salaries),
                      base_max=max(float(c["maxValue"]) for c in salaries), currency=next(iter(currencies)))
    summary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
    if summary:
        result["raw"] = str(summary) + ("\n" + result["raw"] if result["raw"] else "")
    if result["tc_max"] is None and result["base_max"] is not None:
        result["tc_status"] = "base_only"
    return result


def _required(record, key):
    value = record.get(key)
    if value is None or not str(value).strip():
        raise SourceError("Job record missing required field: " + key)
    return str(value)


def _normalize(source, record):
    if not isinstance(record, dict):
        raise SourceError("Each job record must be an object")
    kind = source["kind"]
    job = {"source_id": source["id"], "company": source["company"],
           "department": "", "employment_type": "", "remote": False,
           "posted_at": None}
    if kind == "greenhouse":
        job.update(external_id=_required(record, "id"), title=_required(record, "title"),
                   url=_required(record, "absolute_url"),
                   location=str((record.get("location") or {}).get("name", "")),
                   description=plain_text(record.get("content", "")),
                   department="; ".join(str(d.get("name", "")) for d in record.get("departments", []) if isinstance(d, dict)),
                   posted_at=record.get("first_published"))
        # updated_at is a modification timestamp, never claimed as post date.
        for item in record.get("metadata") or []:
            if isinstance(item, dict) and item.get("name", "").lower() in ("employment type", "time type"):
                job["employment_type"] = str(item.get("value") or "")
        job["remote"] = bool(re.search(r"\bremote\b", job["location"], re.I))
        job["compensation"] = compensation_from_text(job["description"])
    elif kind == "ashby":
        job.update(external_id=str(record.get("id") or urlsplit(_required(record, "jobUrl")).path.rstrip("/").split("/")[-1]),
                   title=_required(record, "title"), url=_required(record, "jobUrl"),
                   location="; ".join(dict.fromkeys([str(record.get("location") or "")] +
                       [str(loc.get("location", "")) for loc in record.get("secondaryLocations", []) if isinstance(loc, dict)])),
                   description=str(record.get("descriptionPlain") or plain_text(record.get("descriptionHtml", ""))),
                   department=str(record.get("department") or record.get("team") or ""),
                   employment_type=str(record.get("employmentType") or ""),
                   posted_at=record.get("publishedAt"))
        workplace = str(record.get("workplaceType") or "").lower()
        # Ashby isRemote includes hybrid; only workplaceType distinguishes it.
        job["remote"] = workplace == "remote" if workplace else bool(record.get("isRemote"))
        job["workplace_type"] = record.get("workplaceType") or "unknown"
        job["compensation"] = _ashby_compensation(record, job["description"])
    else:  # Lever
        categories = record.get("categories") or {}
        if not isinstance(categories, dict):
            raise SourceError("Lever categories must be an object")
        descriptions = [str(record.get("descriptionPlain") or plain_text(record.get("description", "")))]
        descriptions += [plain_text(item.get("text", "") + "\n" + item.get("content", ""))
                         for item in record.get("lists", []) if isinstance(item, dict)]
        descriptions.append(plain_text(record.get("additional", "")))
        locations = categories.get("allLocations") or [categories.get("location", "")]
        if not isinstance(locations, list):
            raise SourceError("Lever allLocations must be a list")
        job.update(external_id=_required(record, "id"), title=_required(record, "text"),
                   url=_required(record, "hostedUrl"), location="; ".join(str(x) for x in locations),
                   description="\n".join(x for x in descriptions if x),
                   department=str(categories.get("department") or categories.get("team") or ""),
                   employment_type=str(categories.get("commitment") or ""),
                   remote=str(record.get("workplaceType", "")).lower() == "remote")
        job["compensation"] = compensation_from_text(job["description"])
        salary = record.get("salaryRange") or {}
        if isinstance(salary, dict) and str(salary.get("interval", "")).lower() in ("per-year-salary", "year", "yearly"):
            low, high = _number(salary.get("min")), _number(salary.get("max"))
            if low is not None and high is not None and low <= high:
                job["compensation"].update(base_min=low, base_max=high, currency=salary.get("currency"),
                                           raw=json.dumps(salary), tc_status="base_only")
    parts = urlsplit(job["url"])
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SourceError("Job URL must be an absolute HTTP(S) URL")
    job["url"] = canonical_url(job["url"])
    return job


def fetch_source(source):
    """Fetch a configured Greenhouse, Ashby or Lever board; manual is a no-op."""
    kind = source.get("kind")
    if kind == "manual":
        return []
    if kind not in ("greenhouse", "ashby", "lever"):
        raise SourceError("Unsupported source kind: " + str(kind))
    for key in ("id", "company", "board"):
        if not source.get(key):
            raise SourceError("Source missing required field: " + key)
    board = str(source["board"])
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", board):
        raise SourceError("Board must be an ATS board token, not a URL")
    token = quote(board, safe="")
    if kind == "greenhouse":
        url = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true".format(token)
    elif kind == "ashby":
        url = "https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true".format(token)
    else:
        url = "https://api.lever.co/v0/postings/{}?mode=json".format(token)
    payload = _request_json(url)
    if kind == "lever":
        records = payload
    else:
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise SourceError("Job board response must contain a jobs list")
        records = payload["jobs"]
    if not isinstance(records, list):
        raise SourceError("Job board jobs must be a list")
    jobs = {}
    for record in records:
        if kind == "ashby" and isinstance(record, dict) and record.get("isListed") is False:
            continue
        try:
            job = _normalize(source, record)
        except (AttributeError, TypeError, KeyError, ValueError) as exc:
            raise SourceError("Malformed {} job record: {}".format(kind, exc)) from exc
        jobs[job_id(job)] = job
    return list(jobs.values())
