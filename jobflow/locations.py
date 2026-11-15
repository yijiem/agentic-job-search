"""Validated employer-location evidence and read-only links to recorded jobs.

An office reference is not a claim that a particular role works at that office.
No address geocoding, office inference, or record writes occur in this module.
"""
import math
import re
from datetime import date
from urllib.parse import urlsplit

KIND = 'company_location'
REVIEW_KIND = 'company_location_review'
REVIEW_STATUSES = ('verified', 'remote_only', 'unverified')
COVERAGE_STATUSES = ('verified', 'address_only', 'remote_only', 'unverified')
RELATIONSHIPS = ('company_office', 'job_site')
PRECISIONS = ('address', 'building')
APPROXIMATE_COORDINATE_METHODS = ('census_address_range', 'openstreetmap_address_point', 'published_address_point')
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
PUBLIC_FIELDS = ('id', 'kind', 'company', 'city', 'address', 'latitude', 'longitude',
                 'precision', 'relationship', 'job_ids', 'source_url',
                 'coordinate_source_url', 'coordinate_method', 'coordinate_note', 'observed_at', 'text')


def _text(value, field, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ValueError('Company location %s must be nonempty text of at most %s characters.' % (field, maximum))
    return value.strip()


def _url(value, field):
    value = _text(value, field, 2048)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                 not parsed.username and not parsed.password and
                 not any(char.isspace() or ord(char) < 32 for char in value))
        parsed.port  # Invalid port syntax is not a usable evidence URL.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Company location %s must be an HTTP(S) URL without credentials.' % field)
    return value


def validate_company_location(record, job_lookup=None):
    """Validate an imported research row, retaining unrelated user metadata.

    ``job_lookup`` is supplied by imports to require existing same-company jobs
    for every explicitly confirmed job-site relationship. Read projections use
    the structural checks and independently match each linked job again.
    """
    row = dict(record)
    if row.get('kind') != KIND:
        raise ValueError('Company location kind must be company_location.')
    if not isinstance(row.get('id'), str) or not IDENTIFIER.fullmatch(row['id']):
        raise ValueError('Company location id must use letters, digits, underscores, and hyphens.')
    for field, maximum in (('company', 200), ('city', 120), ('address', 1000), ('text', 10000)):
        row[field] = _text(row.get(field), field, maximum)
    if row.get('relationship') not in RELATIONSHIPS:
        raise ValueError('Company location relationship must be company_office or job_site.')
    if row.get('precision') not in PRECISIONS:
        raise ValueError('Company location precision must be address or building.')
    observed = row.get('observed_at')
    try:
        if not isinstance(observed, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', observed):
            raise ValueError()
        date.fromisoformat(observed)
    except (TypeError, ValueError):
        raise ValueError('Company location observed_at must be an ISO date (YYYY-MM-DD).') from None
    row['source_url'] = _url(row.get('source_url'), 'source_url')
    sources = row.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('Company locations need a nonempty list of source objects.')
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('Company location sources must be objects.')
        _url(source.get('url'), 'sources.url')
        if 'title' in source and not isinstance(source['title'], str):
            raise ValueError('Company location source titles must be text.')
    source_urls = {source['url'] for source in sources}
    if row['source_url'] not in source_urls:
        raise ValueError('Company location source_url must also appear in sources.')
    has_lat = row.get('latitude') is not None
    has_lng = row.get('longitude') is not None
    if has_lat != has_lng:
        raise ValueError('Company locations need both latitude and longitude, or neither.')
    if has_lat:
        for field, bound in (('latitude', 90), ('longitude', 180)):
            value = row[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -bound <= value <= bound:
                raise ValueError('Company location %s must be a finite number within ±%s.' % (field, bound))
            row[field] = float(value)
        row['coordinate_source_url'] = _url(row.get('coordinate_source_url'), 'coordinate_source_url')
        if row['coordinate_source_url'] not in source_urls:
            raise ValueError('Company location coordinate_source_url must also appear in sources.')
    else:
        row.pop('latitude', None)
        row.pop('longitude', None)
        if row.get('coordinate_source_url'):
            row['coordinate_source_url'] = _url(row['coordinate_source_url'], 'coordinate_source_url')
    if row.get('coordinate_method'):
        if row['coordinate_method'] not in APPROXIMATE_COORDINATE_METHODS + ('employer_map_link',):
            raise ValueError('Company location coordinate_method must be census_address_range, employer_map_link, openstreetmap_address_point, or published_address_point.')
        if not has_lat:
            raise ValueError('Company location coordinate_method requires coordinates.')
        if row['coordinate_method'] in APPROXIMATE_COORDINATE_METHODS and row['precision'] != 'address':
            raise ValueError('Geocoded coordinates require address precision, not building precision.')
    if row.get('coordinate_note'):
        row['coordinate_note'] = _text(row['coordinate_note'], 'coordinate_note', 1000)
    if row.get('coordinate_method') in APPROXIMATE_COORDINATE_METHODS and not row.get('coordinate_note'):
        raise ValueError('Geocoded coordinates require a note explaining their approximation.')
    job_ids = row.get('job_ids', [])
    if not isinstance(job_ids, list) or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in job_ids):
        raise ValueError('Company location job_ids must be a list of valid job IDs.')
    if len(set(job_ids)) != len(job_ids):
        raise ValueError('Company location job_ids must not contain duplicates.')
    if row['relationship'] == 'company_office' and job_ids:
        raise ValueError('Company office references cannot claim explicit job_ids; use job_site with posting evidence.')
    if row['relationship'] == 'job_site' and not job_ids:
        raise ValueError('A posting-confirmed job_site requires explicit job_ids.')
    if job_lookup is not None:
        for job_id in job_ids:
            job = job_lookup(job_id)
            if not job:
                raise ValueError('Company location has unknown job_id: ' + job_id)
            if str(job.get('company', '')).strip().casefold() != row['company'].casefold():
                raise ValueError('Company location job_ids must belong to the same company.')
            if job.get('url') not in source_urls:
                raise ValueError('A confirmed job_site must include every linked employer posting URL in sources.')
            if not _city_compatible(job.get('location'), row['city']):
                raise ValueError('A confirmed job_site must match each linked posting\'s current city.')
    row['job_ids'] = list(job_ids)
    return row


def location_catalog(research):
    """Return only public fields from structurally valid location evidence."""
    result = []
    for record in research:
        if not isinstance(record, dict) or record.get('kind') != KIND:
            continue
        try:
            row = validate_company_location(record)
        except (TypeError, ValueError):
            continue
        public = {field: row[field] for field in PUBLIC_FIELDS if field in row}
        public['sources'] = [{key: source[key] for key in ('url', 'title') if key in source}
                             for source in row['sources']]
        public['worksite_confirmed'] = row['relationship'] == 'job_site'
        public['label'] = ('Posting-confirmed worksite' if public['worksite_confirmed']
                           else 'Company office reference · worksite unconfirmed')
        result.append(public)
    return sorted(result, key=lambda row: (row['company'].casefold(), row['city'].casefold(), row['address'], row['id']))


def validate_location_review(record, location_lookup=None):
    """Validate a dated company-wide research outcome, separate from a pin."""
    row = dict(record)
    if row.get('kind') != REVIEW_KIND:
        raise ValueError('Company location review kind must be company_location_review.')
    if not isinstance(row.get('id'), str) or not IDENTIFIER.fullmatch(row['id']):
        raise ValueError('Company location review requires a valid explicit id.')
    for field, maximum in (('company', 200), ('text', 10000)):
        row[field] = _text(row.get(field), field, maximum)
    if row.get('status') not in REVIEW_STATUSES:
        raise ValueError('Company location review status must be verified, remote_only, or unverified.')
    observed = row.get('observed_at')
    try:
        if not isinstance(observed, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', observed):
            raise ValueError()
        date.fromisoformat(observed)
    except (TypeError, ValueError):
        raise ValueError('Company location review observed_at must be an ISO date (YYYY-MM-DD).') from None
    sources = row.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('Company location reviews require a nonempty list of source objects.')
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('Company location review sources must be objects.')
        _url(source.get('url'), 'sources.url')
        if 'title' in source and not isinstance(source['title'], str):
            raise ValueError('Company location review source titles must be text.')
    ids = row.get('location_ids', [])
    if (not isinstance(ids, list) or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Company location review location_ids must be a list of unique valid identifiers.')
    if row['status'] == 'verified' and not ids:
        raise ValueError('A verified company location review requires location_ids.')
    if row['status'] != 'verified' and ids:
        raise ValueError('Only verified company location reviews may reference location_ids.')
    if location_lookup is not None:
        for location_id in ids:
            location = location_lookup(location_id)
            if not location:
                raise ValueError('Company location review references an unknown location: ' + location_id)
            location = validate_company_location(location)
            if location['company'].casefold() != row['company'].casefold():
                raise ValueError('Company location review location_ids must belong to the same company.')
    row['location_ids'] = list(ids)
    return row


def location_reviews(research, catalog, companies=None, jobs=()):
    """Project company coverage without treating a review as address evidence.

    An existing valid address determines coordinate coverage. A remote-only
    review is used only when no address exists; it never removes saved evidence.
    Synthetic unreviewed entries keep every requested company in the directory.
    Job-site evidence contributes only while a supplied current matching job
    still satisfies the same company, posting URL and city attachment checks.
    """
    sites = [row for row in catalog if row['relationship'] == 'job_site']
    current_site_ids = {row['id'] for job in jobs if job.get('availability') != 'closed'
                        for row in office_locations(job, sites)}
    latest, names, locations = {}, {}, {}
    for row in catalog:
        key = row['company'].casefold()
        names[key] = row['company']
        if row['relationship'] == 'job_site' and row['id'] not in current_site_ids:
            continue
        locations.setdefault(key, []).append(row)
    for record in research:
        if not isinstance(record, dict) or record.get('kind') != REVIEW_KIND:
            continue
        try:
            row = validate_location_review(record)
        except (TypeError, ValueError):
            continue
        key = row['company'].casefold()
        names[key] = row['company']
        previous = latest.get(key)
        if previous is None or (row['observed_at'], row['id']) > (previous['observed_at'], previous['id']):
            latest[key] = row
    if companies is not None:
        names = {str(company).strip().casefold(): str(company).strip() for company in companies if str(company).strip()}
    result = []
    for key, company in sorted(names.items()):
        review, offices = latest.get(key), locations.get(key, [])
        coordinate_count = sum('latitude' in row and 'longitude' in row for row in offices)
        status = ('verified' if coordinate_count else 'address_only') if offices else (
            'remote_only' if review and review['status'] == 'remote_only' else 'unverified')
        public = {field: review[field] for field in ('id', 'kind', 'observed_at', 'text') if review and field in review}
        if not review:
            public.update(kind=REVIEW_KIND, text=('Sourced company address evidence is available; individual role worksites remain unconfirmed.'
                                                 if offices else 'No verified physical office address is recorded for this company.'))
            if offices:
                public['observed_at'] = max(row['observed_at'] for row in offices)
        evidence_sources = review['sources'] if review else [source for row in offices for source in row['sources']]
        seen_sources, sources = set(), []
        for source in evidence_sources:
            if source['url'] not in seen_sources:
                seen_sources.add(source['url'])
                sources.append({field: source[field] for field in ('url', 'title') if field in source})
        public.update(company=company, status=status, review_status=review['status'] if review else None,
                      reviewed=bool(review or offices), location_ids=[row['id'] for row in offices],
                      locations_count=len(offices), coordinates_count=coordinate_count, sources=sources)
        result.append(public)
    return result


def _city_compatible(location, city):
    text, city = str(location or '').casefold(), city.casefold()
    if city == 'san francisco':
        text = re.sub(r'south san francisco|san francisco bay area', '', text)
    if city == 'palo alto':
        text = re.sub(r'east palo alto', '', text)
    # A county, metropolitan region, or broad area does not confirm its city.
    text = re.sub(r'\b(?:greater\s+)?' + re.escape(city) + r'\s+(?:county|region|bay area|metro(?:politan)?(?: area)?)\b', '', text)
    text = re.sub(r'\bgreater\s+' + re.escape(city) + r'\b', '', text)
    return bool(re.search(r'(?<!\w)' + re.escape(city) + r'(?!\w)', text))


def office_locations(job, catalog):
    """Attach evidence without altering the employer's original location text."""
    company = str(job.get('company', '')).strip().casefold()
    remote = bool(job.get('remote')) or bool(re.search(r'\bremote\b', str(job.get('location', '')), re.I))
    result = []
    for row in catalog:
        if row['company'].casefold() != company:
            continue
        if row['relationship'] == 'job_site':
            # Dated evidence stays in the catalog, but a changed posting must
            # not inherit a formerly confirmed worksite merely by stable ID.
            source_urls = {source['url'] for source in row.get('sources', [])}
            if (job.get('id') not in row['job_ids'] or job.get('url') not in source_urls
                    or not _city_compatible(job.get('location'), row['city'])):
                continue
        elif remote or not _city_compatible(job.get('location'), row['city']):
            continue
        result.append(dict(row))
    return result
