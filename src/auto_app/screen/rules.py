"""Deterministic screening for new-grad / entry-level CS jobs (docs/linux-worker.md §2).

A pure function of a job's saved facts and the rules file: no network, no model, no database.
Only explicit evidence excludes; anything uncertain stays "kept" for the user to judge.
Visa sponsorship is a signal for ordering, never an exclusion. The worker stores the verdict as a
view (job_screenings) — never as a decision.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from auto_app.screen.locations import job_location

RULES_VERSION = "7"  # bump when a pattern below changes meaning: every job is screened again

INTERN = re.compile(r"\b(?:intern|interns|internship|internships|co-?op|apprentice|apprenticeship)\b", re.I)
SEASON = re.compile(r"\b(?:summer|fall|autumn|winter|spring)\s*'?(?:20)?\d\d\b", re.I)
NEW_GRAD = re.compile(r"\b(?:new\s*grads?|new\s*graduates?|graduate|entry[\s-]*level|early[\s-]*career|university|"
                      r"college|campus|junior|jr)\b", re.I)
SENIOR = re.compile(
    r"\b(?:senior|sr|staff|principal|lead|manager|director|head\s+of|vp|vice\s+president|distinguished|fellow|"
    r"architect)\b"
    r"|(?<=\s)(?:iii|iv|v|vi)\b(?=\s*(?:$|[,()\-–/|]))"  # "Engineer III", not "V&V"
    r"|\blevel\s*[3-9]\b", re.I)
CS_ROLE = re.compile(
    r"\b(?:software|swe|sde|sdet|developer|programmer|full[\s-]*stack|front[\s-]*end|back[\s-]*end|web|mobile|ios|"
    r"android|data\s+(?:engineer|engineering|scientist|science|analyst|analytics|platform|infrastructure)|"
    r"analytics\s+engineer|business\s+intelligence|machine\s+learning|ml|mle|ai|artificial\s+intelligence|"
    r"deep\s+learning|llm|nlp|natural\s+language|computer\s+vision|research\s+(?:engineer|scientist)|"
    r"applied\s+scientist|cloud|devops|sre|site\s+reliability|platform|infrastructure|security|cybersecurity|cyber|"
    r"appsec|embedded|firmware|fpga|compiler|kernel|distributed|database|"
    r"quant(?:itative)?\s+(?:developer|researcher|engineer|trader|analyst)|game|graphics|blockchain|automation|"
    r"test\s+engineer|qa|quality\s+assurance|computer|it|systems?\s+engineer|solutions?\s+engineer|"
    r"forward[\s-]+deployed)\b", re.I)
NON_CS_ROLE = re.compile(
    r"\b(?:mechanical|civil|structural|chemical|manufacturing|industrial|hvac|construction|electrical|"
    r"power\s+systems|rf|radio\s+frequency|analog|mixed[\s-]*signal|asic|rtl|physical\s+design|pcb|optical|"
    r"photonics|materials|nuclear|aerospace|propulsion|thermal|process\s+engineer|technician|product\s+manager|"
    r"program\s+manager|project\s+manager|product\s+owner|designer|sales|account\s+executive|"
    r"business\s+development|marketing|recruiter|recruiting|human\s+resources|accountant|accounting|"
    r"financial\s+analyst|audit|tax|legal|paralegal|attorney|nurse|clinical|pharmacist|biologist|chemist)\b", re.I)
CS_CATEGORIES = {"software", "software engineering", "ai/ml/data", "data science, ai & machine learning", "quant"}
NON_CS_CATEGORIES = {"hardware", "hardware engineering", "product", "product management"}
LABELS = {"offers sponsorship": "offers", "does not offer sponsorship": "no",
          "u.s. citizenship is required": "citizenship_required"}
_US = r"(?:u\.?\s?s\.?|united\s+states)"
CITIZENSHIP = re.compile(
    rf"{_US}\s+citizen(?:ship)?\s+(?:is\s+)?required|"
    rf"requires?\s+(?:an?\s+)?{_US}\s+citizenship|"
    rf"must\s+(?:be|hold)\s+(?:a\s+)?{_US}\s+citizen|"
    rf"{_US}\s+citizens?\s+only|"
    r"(?:secret|top\s+secret|ts/sci)\s+(?:security\s+)?clearance\s+(?:is\s+)?required|"
    r"security\s+clearance\s+(?:is\s+)?required|"
    r"must\s+be\s+eligible\s+for\s+(?:an?\s+)?(?:security\s+)?clearance|"
    r"must\s+(?:meet|satisfy|comply\s+with)\s+ITAR\s+(?:requirements|regulations)|"
    r"(?:active|current)\s+(?:secret|top\s+secret|ts/sci)\s+(?:security\s+)?clearance|"
    r"(?:ability|able|eligible)\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?(?:active\s+)?"
    rf"(?:{_US}\s+)?(?:government\s+|dod\s+)?(?:security\s+)?clearance|"
    rf"(?:must\s+be|requires?\s+|required\s+to\s+be)\s*(?:an?\s+)?{_US}\s+persons?\b|"
    rf"{_US}\s+person\s+(?:status\s+)?(?:is\s+)?required", re.I)
SPONSORSHIP_NOUN = r"(?:(?:visa|work|employment|immigration|h-?1b)\s+){0,3}sponsorship\b"
NO_SPONSOR = re.compile(
    r"\b(?:(?:not|never|unable\s+to|cannot|can't|won't)\s+"
    r"(?:(?:be|able|to|currently)\s+){0,4}(?:sponsor\b|(?:offer|provide|support)\s+" + SPONSORSHIP_NOUN + r")|"
    r"no\s+" + SPONSORSHIP_NOUN + r"|"
    r"(?:authorized|authorization|eligible|eligibility)\b[^.!?;\n]{0,100}\bwithout\s+"
    r"(?:\w+\s+){0,4}sponsorship\b|"
    r"sponsorship\s+(?:is\s+)?(?:not\s+available|not\s+offered|not\s+provided|unavailable))", re.I)
OFFERS_SPONSOR = re.compile(
    r"\b(?:(?:will|can|able\s+to|happy\s+to|glad\s+to)\s+(?:\w+\s+){0,2}sponsor\b|"
    r"sponsorship\s+(?:is\s+)?(?:available|offered|provided)|"
    r"offers?\s+" + SPONSORSHIP_NOUN + r")", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?:\s+of)?"
                   r"(?:\s+[\w/-]+){0,4}?\s+experience", re.I)
SOFT = re.compile(r"prefer|nice\s+to\s+have|bonus|a\s+plus|desired|ideally|in\s+lieu|instead\s+of|equivalent", re.I)
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
NEGATED_REQUIREMENT = re.compile(
    r"\b(?:not|never)\s+(?:\w+\s+){0,2}(?:required|necessary|needed|mandatory)\b|"
    r"\b(?:do\s+not|does\s+not|don't|doesn't|need\s+not)\s+(?:need|require|have\s+to|be)\b|"
    r"\bno\s+(?:prior\s+|previous\s+)?(?:experience|security\s+clearance|requirement|need)\b|"
    r"\bno\s+more\s+than\b", re.I)
SOFT_HEADING = re.compile(r"^(?:preferred(?:\s+qualifications?)?|nice\s+to\s+have|bonus|desired\s+qualifications?)\s*:?$", re.I)
HARD_HEADING = re.compile(r"^(?:(?:minimum|basic|required)\s+)?(?:qualifications?|requirements?)\s*:?$", re.I)
NON_CANDIDATE_EXPERIENCE = re.compile(
    r"\b(?:our|the)\s+(?:company|team|business|founders?|engineers?)\b|\bwe\s+(?:have|bring)\b|"
    r"^\s*(?:with|for|after|over|nearly|almost)\s+(?:over\s+|more\s+than\s+)?\d{1,3}\+?\s+years?\s+of\s+"
    r"(?:\w+\s+){0,2}experience\s*,|"
    # A named employer has tenure; 'you have' and 'candidates must have' are requirements.
    r"\b(?!(?:candidate|applicant|developer|engineer|employee|person|individual)\b)"
    r"(?-i:[A-Z][\w&'-]*)\s+(?:has|brings?)\s+(?:over\s+|more\s+than\s+|nearly\s+)?"
    r"\d{1,3}\+?\s+years?\s+of\s+(?:\w+\s+){0,2}experience\b", re.I)


def _sentences(text: str):
    # Do not split the U.S. abbreviation before its citizenship noun.
    text = re.sub(r"\bU\.\s*S\.", "US", text, flags=re.I).replace("’", "'")
    return SENTENCE.split(text)


@dataclass(frozen=True)
class JobFacts:
    job_id: int
    titles: tuple[str, ...]
    employment_types: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    description: str = ""
    sponsorship_labels: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    closed_at_source: bool = False  # every apply link's employer API says the job is gone
    locations: tuple[str, ...] = ()  # every posting's raw locations, as published
    countries: tuple[str, ...] = ()  # structured ISO country codes (Lever boards)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ScreeningRules:
    exclude_internships: bool = True
    exclude_senior: bool = True
    exclude_non_cs: bool = True
    exclude_citizenship_required: bool = True
    max_years_required: int | None = 3  # a stated requirement of this many years or more excludes; None = off
    extra_cs_terms: tuple[str, ...] = ()  # more words that make a title a CS role
    extra_non_cs_terms: tuple[str, ...] = ()  # more words that make a title clearly not a CS role
    us_only: bool = True  # exclude jobs whose explicit locations are all outside the US

    @property
    def version(self) -> str:
        data = json.dumps({"v": RULES_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Screening:
    outcome: str  # kept | excluded
    reasons: tuple[str, ...]
    signals: dict


def years_required(description: str) -> int | None:
    """Combine mandatory constraints, keeping the least demanding explicit alternative route."""
    found = []
    preferred_section = False
    for sentence in _sentences(description or ""):
        sentence = sentence.strip().strip("#*• ")
        if SOFT_HEADING.fullmatch(sentence):
            preferred_section = True
        elif HARD_HEADING.fullmatch(sentence):
            preferred_section = False
        if preferred_section or SOFT.search(sentence) or NEGATED_REQUIREMENT.search(sentence) \
                or NON_CANDIDATE_EXPERIENCE.search(sentence):
            continue
        # A numeric alternative can share its unit: "3 or 4 years of experience".
        sentence = re.sub(r"\b(\d{1,2})\s+or\s+(\d{1,2})(?=\s*\+?\s*(?:years?|yrs?)\b)",
                          lambda m: str(min(int(m[1]), int(m[2]))), sentence, flags=re.I)
        # Only split an OR that introduces a numeric requirement or degree route.
        # "experience in C++ or Python and 2 years ..." still has two mandatory constraints.
        continuation = bool(re.match(r"^or\b", sentence, re.I))
        alternatives = re.split(
            r"\bor\s+(?=(?:an?\s+)?(?:\d|bachelor\b|master\b|master's\b|bachelor's\b|"
            r"ph\.?d\b|doctorate\b|doctoral\b))", re.sub(r"^or\s+", "", sentence, flags=re.I), flags=re.I)
        route_years = [[int(m.group(1)) for m in YEARS.finditer(route)] for route in alternatives]
        degree_only = re.compile(r"(?:an?\s+)?(?:bachelor'?s?|master'?s?|doctoral)\s+degree[.!;]?|"
                                 r"(?:an?\s+)?(?:ph\.?d\.?|doctorate)[.!;]?", re.I)
        if continuation and found and degree_only.fullmatch(alternatives[0].strip()):
            found[-1] = 0
            continue
        if not any(route_years):
            continue
        # A complete degree-only route establishes no hard experience floor. A fragment
        # such as "Bachelor's" in "Bachelor's or Master's degree and 5 years" does not.
        shared_degree_choice = any(
            re.search(r"\b(?:bachelor|master)'?s?\b", route[list(YEARS.finditer(route))[-1].end():], re.I)
            for route, values in zip(alternatives[:-1], route_years[:-1]) if values)
        if not shared_degree_choice and all(values or degree_only.fullmatch(route.strip())
                                            for route, values in zip(alternatives, route_years)):
            required = min(max(values, default=0) for values in route_years)
        else:
            required = max(value for values in route_years for value in values)
        if continuation and found:
            found[-1] = min(found[-1], required)
        else:
            found.append(required)
    return max(found) if found else None


def screen(facts: JobFacts, rules: ScreeningRules) -> Screening:
    title = " / ".join(t for t in facts.titles if t)
    new_grad = bool(NEW_GRAD.search(title))
    reasons = []
    if rules.exclude_internships and _internship(facts, title, new_grad):
        reasons.append("internship")
    if rules.exclude_senior and not new_grad and any(_senior(t) for t in facts.titles):
        reasons.append("senior_title")
    role = _role(facts, title, rules)
    if rules.exclude_non_cs and role == "non_cs":
        reasons.append("not_cs_role")
    sponsorship = _sponsorship(facts)
    if rules.exclude_citizenship_required and sponsorship == "citizenship_required":
        reasons.append("citizenship_required")
    years = years_required(facts.description)
    if rules.max_years_required is not None and years is not None \
            and years >= rules.max_years_required:
        reasons.append(f"experience_required:{years}")
    if facts.closed_at_source:
        reasons.append("closed_at_source")
    location = job_location(facts.locations, facts.countries)
    if rules.us_only and location == "non_us":
        reasons.append("outside_us")
    signals = {"role": role, "sponsorship": sponsorship, "years_required": years, "new_grad_title": new_grad,
               "has_description": bool(facts.description.strip()), "location": location}
    return Screening("excluded" if reasons else "kept", tuple(reasons), signals)


def _internship(facts: JobFacts, title: str, new_grad: bool) -> bool:
    if any(value.strip().lower() == "internship" for value in facts.employment_types) or INTERN.search(title):
        return True
    if SEASON.search(title) and not new_grad:  # "Software Engineer (Summer 2027)"
        return True
    return bool(facts.sources) and all(INTERN.search(source.replace("_", " ")) for source in facts.sources)


def _senior(title: str) -> bool:
    # Member of Technical Staff is used for multiple levels, including graduates.
    title = re.sub(r"\bmember\s+of\s+(?:the\s+)?technical\s+staff\b", "engineer", title, flags=re.I)
    # A team/product suffix can contain job-level words without describing this role.
    parts = re.split(r"\s+(?:[-–|])\s+|,\s*", title)
    if SENIOR.search(parts[0]):
        return True
    return any(SENIOR.fullmatch(part.strip(" .()")) or
               re.fullmatch(r"(?:iii|iv|v|vi)", part.strip(" .()"), re.I) or
               (SENIOR.search(part) and re.search(r"\b(?:engineer|developer|scientist|architect)\b", part, re.I))
               for part in parts[1:])


def _terms(terms: tuple[str, ...]) -> re.Pattern | None:
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.I) if terms else None


def _role(facts: JobFacts, title: str, rules: ScreeningRules) -> str:
    categories = {c.strip().lower() for c in facts.categories if c}
    extra_cs, extra_non_cs = _terms(rules.extra_cs_terms), _terms(rules.extra_non_cs_terms)
    if CS_ROLE.search(title) or (extra_cs and extra_cs.search(title)):
        return "cs"
    if NON_CS_ROLE.search(title) or (extra_non_cs and extra_non_cs.search(title)):
        return "non_cs"
    if categories & CS_CATEGORIES:
        return "cs"
    if categories & NON_CS_CATEGORIES:
        return "non_cs"
    return "unknown"


def _sponsorship(facts: JobFacts) -> str:
    labels = {LABELS.get(label.strip().lower()) for label in facts.sponsorship_labels} - {None}
    sentences = _sentences(facts.description or "")
    required = any(CITIZENSHIP.search(s) and "?" not in s
                   and not (NEGATED_REQUIREMENT.search(s) or SOFT.search(s))
                   for s in sentences)
    if "citizenship_required" in labels or required:
        return "citizenship_required"
    # Questions and conditional language supply no affirmative sponsorship evidence.
    concrete = [s for s in sentences if not re.search(
        r"\?|\b(?:guarantee|might|may|possibly|not\s+unavailable)\b", s, re.I)]
    if any(NO_SPONSOR.search(s) for s in concrete):  # employer words win over repository labels
        return "no"
    if "offers" in labels or any(OFFERS_SPONSOR.search(s) for s in concrete):
        return "offers"
    return "no" if "no" in labels else "unknown"


def load_rules(path: Path | None) -> ScreeningRules:
    """$AUTO_APP_HOME/config/screening.json; a missing file means the defaults."""
    if path is None or not Path(path).exists():
        return ScreeningRules()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read screening rules {path}: {exc}") from exc
    return rules_from_dict(data, where=f"screening rules {path}")


def rules_from_dict(data, *, where: str = "screening rules") -> ScreeningRules:
    if not isinstance(data, dict):
        raise ValueError(f"Cannot read {where}: expected an object")
    unknown = set(data) - {f.name for f in fields(ScreeningRules)}
    if unknown:
        raise ValueError(f"Cannot read {where}: unknown keys {sorted(unknown)}")
    values = {}
    for key, value in data.items():
        if key.startswith("extra_"):
            if not isinstance(value, list) or any(not isinstance(t, str) or not t.strip() for t in value):
                raise ValueError(f"Cannot read {where}: {key} must be a list of nonempty strings")
            values[key] = tuple(t.strip() for t in value)
        elif key == "max_years_required":
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"Cannot read {where}: max_years_required must be a positive "
                                 f"integer or null")
            values[key] = value
        elif type(value) is not bool:
            raise ValueError(f"Cannot read {where}: {key} must be true or false")
        else:
            values[key] = value
    return ScreeningRules(**values)
