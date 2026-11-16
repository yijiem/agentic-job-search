"""Derived public salary-reference links; never estimates listing compensation."""
import re
from datetime import datetime
from urllib.parse import urlsplit


DIRECTORY = 'https://www.levels.fyi/companies'
BENCHMARK_NOTE = ('Reported compensation benchmark; not a salary quote for this opening. '
                  'Check location, currency and submission dates on Levels.fyi.')
SLUG = r'[a-z0-9][a-z0-9-]*'


def company_key(value):
    return ' '.join(str(value or '').casefold().split())


def valid_reference_url(value, scope):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or c == '\\' for c in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if (parsed.scheme != 'https' or parsed.netloc != 'www.levels.fyi'
            or parsed.query or parsed.fragment):
        return False
    patterns = {
        'company': r'/companies/' + SLUG + r'(?:/salaries)?',
        'company_role': r'/companies/' + SLUG + r'/salaries/' + SLUG,
        'level': r'/companies/' + SLUG + r'/salaries/' + SLUG + r'/levels/' + SLUG,
        'directory': r'/companies',
    }
    return scope in patterns and bool(re.fullmatch(patterns[scope], parsed.path))


def reference_index(notes):
    """Only explicitly imported company-reference records participate."""
    result = {}
    for note in sorted(notes, key=lambda row: (str(row.get('observed_at', '')), row.get('id', ''))):
        if note.get('kind') != 'levels_fyi_company_reference':
            continue
        company, data = note.get('company'), note.get('levels_fyi')
        try:
            datetime.fromisoformat(str(note.get('observed_at', '')).replace('Z', '+00:00'))
        except ValueError:
            continue
        if company and isinstance(data, dict):
            result[company_key(company)] = {**data, 'observed_at': note.get('observed_at')}
    return result


def role_candidates(title):
    """Select a role family, not an employer grade or salary."""
    value = str(title or '').casefold()
    if re.search(r'\b(?:hardware|electrical|mechanical|thermal|manufacturing|industrial(?:ization)?|'
                 r'supplier|pre[- ]?sales|sales|solutions?|support|field|it)\b', value):
        return []
    specific = (
        (r'\b(?:engineering manager|director|vice president|vp)\b', 'software-engineering-manager'),
        (r'\bdata scientist\b', 'data-scientist'),
        (r'\bproduct manager\b', 'product-manager'),
        (r'\b(?:product|ux) designer\b', 'product-designer'),
        (r'\b(?:hardware|electrical|mechanical)\b', None),
        (r'\bdata engineer\b', 'data-engineer'),
        (r'\b(?:site reliability|sre)\b', 'site-reliability-engineer'),
        (r'\bsecurity\b.*\bengineer\b', 'security-engineer'),
        (r'\b(?:machine learning|ml)\b.*\bengineer\b', 'machine-learning-engineer'),
    )
    for pattern, role in specific:
        if re.search(pattern, value):
            if role in ('software-engineering-manager', 'data-scientist', 'product-manager', 'product-designer') or role is None:
                return [role] if role else []
            return [role, 'software-engineer']
    if (re.search(r'\b(?:software|back[- ]?end|front[- ]?end|full[- ]?stack|platform|infrastructure|'
                  r'cloud|database|distributed|storage|compute|production|research)\b', value)
            and re.search(r'\bengineer(?:ing)?\b', value)) or re.search(r'\bmember of technical staff\b', value):
        return ['software-engineer']
    return []


def explicit_level_codes(title):
    # Restrict to literal employer-style codes in the title. Senior, Staff,
    # years of experience, and Roman numerals do not establish a company grade.
    value = str(title or '')
    codes = set()
    for match in re.finditer(r'\b(?:IC\s*[1-9][0-9]?|[LPE][1-9][0-9]?)\b', value, re.I):
        if re.search(r'\b(?:layer|tier|osi)\s*$', value[:match.start()], re.I):
            continue
        if re.match(r'\s+(?:network(?:ing)?|routing|support|protocol|switching|compiler)\b', value[match.end():], re.I):
            continue
        codes.add(match.group().upper().replace(' ', ''))
    return codes


def salary_reference(job, index):
    company = str(job.get('company', ''))
    data = index.get(company_key(company), {})
    result = {'url': DIRECTORY, 'scope': 'directory', 'label': 'Find company on Levels.fyi',
              'note': 'A company salary page has not been verified. ' + BENCHMARK_NOTE,
              'observed_at': data.get('observed_at'), 'company': company,
              'role': None, 'level': None}
    company_url = data.get('company_url')
    if not valid_reference_url(company_url, 'company'):
        return result
    # Every more-specific destination must belong to this same verified company.
    company_path = '/'.join(urlsplit(company_url).path.split('/')[:3])
    benchmark_note = BENCHMARK_NOTE
    if data.get('salary_data_status') == 'insufficient':
        benchmark_note = 'Levels.fyi currently indicates insufficient salary submissions. ' + benchmark_note
    result.update(url=company_url, scope='company', label='Company salaries (all roles)',
                  note='Role and level are unconfirmed. ' + benchmark_note)
    available = data.get('available_roles', {})
    available = available if isinstance(available, dict) else {}
    available = dict(available)
    if data.get('software_engineer_url'):
        available.setdefault('software-engineer', data['software_engineer_url'])
    roles = role_candidates(job.get('title'))
    for role in roles:
        url = available.get(role)
        if not valid_reference_url(url, 'company_role') or url != company_path_url(company_path) + '/salaries/' + role:
            continue
        role_name = role.replace('-', ' ').title()
        result.update(url=url, scope='company_role', role=role_name,
                      label=role_name + ' salaries (all levels)',
                      note=('This is a broader software-engineering benchmark. ' if role != roles[0] else '')
                      + 'Exact employer level is unconfirmed. ' + benchmark_note)
        codes = explicit_level_codes(job.get('title'))
        # Multiple codes may describe an opening spanning grades: use all levels.
        if len(codes) == 1:
            candidates = []
            levels = data.get('levels', [])
            for level in levels if isinstance(levels, list) else []:
                if not isinstance(level, dict) or level.get('role') != role:
                    continue
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}', str(level.get('label', ''))):
                    continue
                aliases = level.get('aliases', [])
                aliases = aliases if isinstance(aliases, list) else []
                names = {str(x).upper().replace(' ', '') for x in [level.get('label', '')] + aliases}
                target = level.get('url')
                if codes & names and valid_reference_url(target, 'level') and target.startswith(url + '/levels/'):
                    candidates.append(level)
            unique = {item['url']: item for item in candidates}
            if len(unique) == 1:
                level = next(iter(unique.values()))
                result.update(url=level['url'], scope='level', level=str(level['label']),
                              label=role_name + ' · ' + str(level['label']),
                              note='Level code explicitly appears in the job title and matches the linked Levels.fyi grade. ' + benchmark_note)
        return result
    return result


def company_path_url(path):
    return 'https://www.levels.fyi' + path
