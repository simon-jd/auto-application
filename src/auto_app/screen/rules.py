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
from functools import lru_cache
from pathlib import Path

from auto_app.screen.locations import job_location

RULES_VERSION = "9"  # bump when a pattern below changes meaning: every job is screened again

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
# A software skill named anywhere in a description. "C++" and "C#" end in punctuation, so a trailing \b would
# never match them before a space or period.
SOFTWARE_EVIDENCE = re.compile(
    r"(?<!\w)(?:c\+\+|c#)(?!\w)|"
    r"\b(?:python|java|javascript|typescript|golang|rust|sql|scala|kotlin|swift|ruby|php|cobol|mainframe|"
    r"programming|coding|computer\s+science|data\s+structures|algorithms?|apis?|git|github|linux|unix|kubernetes|"
    r"docker|aws|gcp|azure|cloud|machine\s+learning|deep\s+learning|pytorch|tensorflow|llms?|embedded|firmware|"
    r"rtos|microcontrollers?|scripting|software\s+(?:development|engineering|design|stack|systems?)|"
    r"full[\s-]?stack|back[\s-]?end|front[\s-]?end|react|node(?:\.js)?|ci/cd|devops|databases?)\b", re.I)
# A title part made only of level words: "Sr. Staff", "Senior Staff", "Principal Lead".
LEVEL_PART = re.compile(r"(?:(?:senior|sr|staff|principal|lead)\.?\s*)+", re.I)
CS_CATEGORIES = {"software", "software engineering", "ai/ml/data", "data science, ai & machine learning", "quant"}
NON_CS_CATEGORIES = {"hardware", "hardware engineering", "product", "product management"}
LABELS = {"offers sponsorship": "offers", "does not offer sponsorship": "no",
          "u.s. citizenship is required": "citizenship_required"}
_US = r"(?:u\.?\s?s\.?|united\s+states)"
_Q = r"[\"“”']?"  # a defined term is often quoted: “U.S. Person”
_ITEM = r"(?:\(?(?:[ivx]+|[a-z]|\d)\)\s*)?"  # an enumerated list's "(i)", "(A)" or "1)"
_RESIDENT = r"(?:active\s+)?(?:lawful\s+)?(?:green\s*card|permanent\s+resid)\w*(?:\s*holders?)?"
# Requirements to be a U.S. citizen or U.S. person (ITAR/EAR), read one sentence at a time.
CITIZENSHIP = re.compile(
    rf"{_US}\s+citizen(?:ship)?\s+(?:is\s+)?required|"
    rf"requires?\s+(?:an?\s+)?{_US}\s+citizenship|"
    # "applicant must be a (i) U.S. citizen", "must either be (A) a U.S. person", "must qualify as a U.S. person"
    rf"must\s+(?:(?:therefore|either|also)\s+){{0,2}}(?:be|hold|qualify\s+as)\s+(?:one\s+of\s+the\s+following:?\s*)?"
    rf"(?:either\s+)?{_ITEM}(?:an?\s+)?{_ITEM}{_Q}{_US}\s+(?:citizen|person|national)|"
    rf"{_US}\s+(?:citizens?|persons?){_Q}\s+only\b|{_US}\s+citizens?\s*(?:or|and|and/or|/)\s*{_RESIDENT}\s+only\b|"
    rf"(?<!not\s)\bonly\s+(?:for\s+|to\s+)?{_Q}{_US}\s+(?:citizens?|persons?)|"
    rf"(?<!not\s)\b(?:restricted|limited)\s+to\s+{_Q}{_US}\s+(?:citizens?|persons?)|"
    rf"open\s+to\s+{_Q}{_US}\s+(?:citizens?|persons?){_Q}(?:\s*\([^)]*\))?\s+only\b|"
    rf"{_US}\s+citizen(?:ship|s)?\s*(?:or|and|and/or|/)\s*{_RESIDENT}\s*\)?\s*(?:is\s+|are\s+)?required|"
    rf"requir\w*\s+(?:\w+\s+){{0,3}}?{_Q}{_US}\s+persons?{_Q}\s+status|"
    rf"(?:must\s+be|requires?\s+|required\s+to\s+be)\s*(?:an?\s+)?{_Q}{_US}\s+persons?\b|"
    r"requires?\s+(?:the\s+)?(?:successful\s+|selected\s+)?(?:candidate|applicant|incumbent|individual|hire)s?\s+to\s+be\s+"
    rf"(?:an?\s+)?{_Q}{_US}\s+(?:citizen|person)|"
    rf"{_US}\s+persons?\s+(?:status\s+)?(?:is\s+)?required|"
    # Access to export-controlled information with no export-license route.
    r"(?:contingent|conditioned|requir\w*|must)\b[^.]{0,200}?\bwithout\s+(?:obtaining\s+|requiring\s+|needing\s+|"
    r"additional\s+|the\s+need\s+for\s+)?(?:an?\s+)?(?:export\s+)?licens\w*|"
    r"must\s+be\s+eligible\s+for\s+(?:an?\s+)?(?:security\s+)?clearance|"
    r"must\s+(?:meet|satisfy|comply\s+with)\s+ITAR\s+(?:requirements|regulations)|"
    r"(?:ability|able|eligible)\s+to\s+obtain\s+(?:and\s+maintain\s+)?(?:an?\s+)?(?:active\s+)?"
    rf"(?:{_US}\s+)?(?:government\s+|dod\s+)?(?:security\s+)?clearance", re.I)
# A bullet that is only a status: "- U.S. Person status (ITAR requirement)", "- U.S. Citizen or Permanent Resident".
STATUS_ITEM = re.compile(
    rf"{_Q}{_US}\s+persons?{_Q}(?:\s+status)?(?:\s+only|\s+required)?(?:\s*\([^)]*\))?|"
    rf"{_US}\s+citizen(?:ship)?(?:\s*(?:,|or|and/or|and|/)\s*(?:an?\s+)?{_RESIDENT}(?:\s+(?:status|of\s+the\s+{_US}))?)+"
    r"(?:\s*\([^)]*\))?(?:\s+only|\s+required)?", re.I)
# A security clearance by name: "Secret clearance", "TS/SCI clearance", "U.S. security clearance" — not
# "customs clearance", "an active clearance" (pay differentials) or a Public Trust suitability check.
_LEVEL = r"(?:top[\s-]*secret|secret|ts|sci|level\s+[ql])"
CLEARANCE = re.compile(rf"\b(?:{_LEVEL}|security|government|federal|dod|doe|{_US})[\"”')]?\s+clearances?\b", re.I)
LEVEL_CLEARANCE = re.compile(rf"\b{_LEVEL}[\"”')]?\s+clearances?\b", re.I)
CLEARANCE_CUE = re.compile(r"\b(?:requir(?:e|es|ed|ing)|must|needs?\s+(?:to|an?|the)|ability|able|eligib\w*|obtain\w*|"
                           r"possess\w*|hold|holds|holding|have|has|maintain\w*|active|current(?:ly)?|willing\w*|"
                           r"condition)\b", re.I)
PAY = re.compile(r"\b(?:compensation|salary|salaries|pay\s+(?:range|rate|scale)|base\s+pay|differential)\b", re.I)
# "Security Clearance:" on its own line, with the level on the next: the two are read as one sentence.
CLEARANCE_HEADING = re.compile(r"(?:(?:security|minimum)\s+)?clearance(?:\s+(?:required|requirements?|level|type))?"
                               r"(?:\s+to\s+start)?\s*:?", re.I)
CLEARANCE_VALUE = re.compile(r"\bclearance(?:\s+(?:required|requirements?|level|type))?(?:\s+to\s+start)?\s*:?\s*"
                             rf"(?:active\s+|current\s+)?(?:{_US}\s+)?(?:top[\s-]*secret|ts\s*/\s*sci|ts|secret)\b", re.I)
# The employer's clearance, an airport badge, or HR work on clearances: not the candidate's requirement.
OTHER_CLEARANCE = re.compile(r"\b(?:facility|tsa\)?|airport|badge)\s+(?:security\s+)?clearances?\b|"
                             r"\bclearances?\s+(?:processing|paperwork|administration)\b", re.I)
NO_CLEARANCE = re.compile(r"\b(?:none|not\s+applicable|n/a)\b", re.I)
# A sentence that also admits non-citizens or visa holders restricts nothing: "only U.S. citizens and lawfully
# authorized non-U.S. citizens".
NON_EXCLUSIVE = re.compile(rf"\bnon-?\s*(?:{_US}\s+)?(?:citizens?|persons?)\b|\b(?:h-?1b|opt|ead|work\s+visas?|"
                           r"visa\s+holders?)\b", re.I)
# A preferred section's heading starts with one of these words ("Nice to Have", "Ways to stand out from the
# crowd"), or ends with a colon ("These Qualifications Would Be Nice to Have:"); "Rust is a plus" is an item.
PREFERRED_HEADING = re.compile(r"(?:prefer\w*|desir\w*|bonus|nice[\s-]+to[\s-]+haves?|pluses|good\s+to\s+have|"
                               r"ways\s+to\s+stand\s+out)\b", re.I)
PREFERRED_WORD = re.compile(r"\b(?:prefer\w*|desir\w*|bonus|nice[\s-]+to[\s-]+haves?|a\s+plus|pluses|stand\s+out|"
                            r"good\s+to\s+have)\b", re.I)
EXPLICIT = re.compile(r"\b(?:required|requires?|must|mandatory)\b", re.I)
BULLETS = " -–•*·▪"
TITLE_CLEARANCE = re.compile(
    r"(?<![-\w])cleared\b|(?<!customs\s)\bclearance\b|\bts\s*/\s*sci\b|\btop[\s-]*secret\b|\bpolygraph\b|"
    r"(?:w/|with|full\s+scope|[-–(])\s*poly\b|\bactive\s+secret\b|[-–/(]\s*secret\s*(?:[-–/)]|$)|"
    rf"\b{_US}\s+(?:citizen(?:ship|s)?|persons?)\b", re.I)
# Conditional wording: the requirement applies to some roles, or a license may be obtained instead.
HEDGE = re.compile(r"\b(?:may|might|could|possibly|potentially|typically|generally|so\s+designated|"
                   r"if\s+(?:applicable|required|needed|necessary)|where\s+(?:required|applicable|necessary|permitted)|"
                   r"(?:certain|some)\s+(?:positions|roles|jobs))\b", re.I)
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
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|~|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?:\s+of)?"
                   r"(?:\s+[\w/-]+){0,4}?\s+experience", re.I)
SOFT = re.compile(r"prefer|nice\s+to\s+have|bonus|a\s+plus|desired|ideally|in\s+lieu|instead\s+of|equivalent", re.I)
SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
NEGATED_REQUIREMENT = re.compile(
    r"\b(?:not|never)\s+(?:\w+\s+){0,2}(?:required|necessary|needed|mandatory)\b|"
    r"\b(?:do\s+not|does\s+not|don't|doesn't|need\s+not)\s+(?:need|require|have\s+to|be)\b|"
    r"\bno\s+(?:prior\s+|previous\s+)?(?:experience|security\s+clearance|requirement|need)\b|"
    r"\bno\s+more\s+than\b", re.I)
# "Meet one of the following:" opens a list of alternative routes; each item names a credential.
ONE_OF = re.compile(r"\b(?:one|any|either)\s+of\s+the\s+following\b", re.I)
CREDENTIAL = re.compile(r"\b(?:degree|diploma|bachelor\w*|master\w*|ph\.?d|doctora\w*|associate\w*|apprenticeship|"
                        r"boot\s*camp|certificat\w*|program|ged)\b", re.I)
DEGREE_ROUTE = re.compile(r"(?:an?\s+)?(?:(?:bachelor|master|associate)'?s?\s+(?:degree|of\s+(?:science|arts|engineering))|"
                          r"ph\.?d\b|doctorate|doctoral\s+degree)", re.I)
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
DEGREE_ONLY = re.compile(r"(?:an?\s+)?(?:bachelor'?s?|master'?s?|doctoral)\s+degree[.!;]?|"
                         r"(?:an?\s+)?(?:ph\.?d\.?|doctorate)[.!;]?", re.I)
OR_START = re.compile(r"^or\b", re.I)
# Shortcuts that skip only text which cannot match (re.I like the patterns they guard, so the same case folding).
# Every match citizenship_evidence returns on, and every clearance heading, contains one of these words;
CITIZENSHIP_WORD = re.compile(r"citizen|person|national|licens|clearance|itar", re.I)
# every NO_SPONSOR or OFFERS_SPONSOR match contains "sponsor", and every YEARS match a digit.
SPONSOR_WORD = re.compile(r"sponsor", re.I)
DIGIT = re.compile(r"\d")


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
    # Opt-in noise rule: a job whose title names no role and whose description names no software skill.
    exclude_unknown_without_software_evidence: bool = False
    # The user's graduation, "YYYY-MM": a job that explicitly starts, or wants graduates, before it is excluded.
    graduation: str | None = None

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
    return _years_required(_sentences(description or ""))


def _years_required(sentences) -> int | None:
    found = []
    preferred_section = False
    one_of = None  # inside a "one of the following" list: False, then the index in `found` of its least route
    for sentence in sentences:
        sentence = sentence.strip().strip("#*• ")
        if one_of is not None and sentence.strip("-– ") and not CREDENTIAL.search(sentence):
            one_of = None  # the list ends at the first item that is not an education route
        if ONE_OF.search(sentence):
            one_of = False
        if SOFT_HEADING.fullmatch(sentence):
            preferred_section = True
        elif HARD_HEADING.fullmatch(sentence):
            preferred_section = False
        if not DIGIT.search(sentence) and not (found and OR_START.match(sentence)):
            continue  # no number of years here, and no degree route to an earlier requirement
        if preferred_section or SOFT.search(sentence) or NEGATED_REQUIREMENT.search(sentence) \
                or NON_CANDIDATE_EXPERIENCE.search(sentence):
            continue
        # A numeric alternative can share its unit: "3 or 4 years of experience".
        sentence = re.sub(r"\b(\d{1,2})\s+or\s+(\d{1,2})(?=\s*\+?\s*(?:years?|yrs?)\b)",
                          lambda m: str(min(int(m[1]), int(m[2]))), sentence, flags=re.I)
        # Only split an OR that introduces a numeric requirement or degree route.
        # "experience in C++ or Python and 2 years ..." still has two mandatory constraints.
        continuation = bool(OR_START.match(sentence))
        alternatives = re.split(
            r"\bor\s+(?=(?:an?\s+)?(?:\d|bachelor\b|master\b|master's\b|bachelor's\b|"
            r"ph\.?d\b|doctorate\b|doctoral\b))", re.sub(r"^or\s+", "", sentence, flags=re.I), flags=re.I)
        route_years = [[int(m.group(1)) for m in YEARS.finditer(route)] for route in alternatives]
        if continuation and found and DEGREE_ONLY.fullmatch(alternatives[0].strip()):
            found[-1] = 0
            continue
        if not any(route_years):
            continue
        # A complete degree-only route establishes no hard experience floor. A fragment
        # such as "Bachelor's" in "Bachelor's or Master's degree and 5 years" does not.
        shared_degree_choice = any(
            re.search(r"\b(?:bachelor|master)'?s?\b", route[list(YEARS.finditer(route))[-1].end():], re.I)
            for route, values in zip(alternatives[:-1], route_years[:-1]) if values)
        # "A Bachelor's degree in CS or 3+ years of experience": a degree route in a named field is an
        # alternative only when no experience route names a degree ("BS in CS or MS in CS and 3 years" is not).
        experience_or_degree = not any(CREDENTIAL.search(route) for route, values in zip(alternatives, route_years)
                                       if values)
        if not shared_degree_choice and all(
                values or DEGREE_ONLY.fullmatch(route.strip())
                or (experience_or_degree and DEGREE_ROUTE.match(route.strip(" -–")))
                for route, values in zip(alternatives, route_years)):
            required = min(max(values, default=0) for values in route_years)
        else:
            required = max(value for values in route_years for value in values)
        if one_of is not None:
            if one_of is False:
                found.append(required)
                one_of = len(found) - 1
            else:
                found[one_of] = min(found[one_of], required)
        elif continuation and found:
            found[-1] = min(found[-1], required)
        else:
            found.append(required)
    return max(found) if found else None


# ---- start dates and graduation classes (the graduation rule) ------------------------------------------------
_MONTH = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
          r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")
PERIOD = re.compile(rf"\b(?:(?P<month>{_MONTH})\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?|"
                    r"(?P<season>spring|summer|fall|autumn|winter)\s+(?:of\s+)?|(?P<quarter>q[1-4])\s+)?"
                    r"'?(?<![\d/\-])(?P<year>20\d\d)(?![/\-]?\d)", re.I)  # not in 11/18/2026, 2026-11-10, Req 2025-04567
MONTH_NUMBER = {name: number for number, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")), start=1) for name in names}
SEASON_END = {"spring": 5, "summer": 8, "fall": 11, "autumn": 11}  # winter Y runs into Y+1: its end is Feb Y+1
# "start date: January 2027", "starting summer 2026" (not "startup", "starting salary", "starting at $90k")
START_THEN = re.compile(r"\b(?:start(?:s|ing)?|begin(?:s|ning)?)\b(?!\s*-?\s*ups?\b)"
                        r"(?!\s+(?:salary|salaries|pay|rate|base|compensation|wage|range|point|at)\b)(?P<rest>.{0,80})",
                        re.I)
THEN_START = re.compile(PERIOD.pattern + r"\s+start(?:\s+date)?\b", re.I)  # "a Winter 2026 start"
# A deadline needs its connector right after the word: "graduate by", "graduating between", "graduation date of"
# (not "our graduate program, launched in 2025").
GRADUATING = re.compile(r"\bgraduat(?:e|es|ed|ing|ion)\b(?:\s+date)?\s*(?:(?:is|of|:|-|–)\s*|(?:between|by|before|in|"
                        r"from|on|during|through|prior\s+to|no\s+later\s+than)\s+)(?P<rest>.{0,60})|"
                        r"\bdegree\b[^.]{0,40}?\b(?:complet\w*|conferr\w*)\b(?P<rest2>.{0,60})", re.I)
# A start word about the hiring process, the posting, pay, benefits or the law is not the job's start date:
# "interviews begin in November 2026", "posting start date", "401(k) match beginning in 2026".
NOT_THE_JOB = re.compile(r"\b(?:applica\w*|apply|interview\w*|postings?|posted|review\w*|recruit\w*|deadline|pay|"
                         r"salar\w*|compensation|wages?|bonus\w*|benefits?|401\s*\(?k\)?|match|pto|leave|insurance|"
                         r"eligib\w*|enroll\w*|law|laws|effective|fiscal|hiring\s+events?|career\s+fairs?)\b", re.I)
NEGATED = re.compile(r"\b(?:not|never|ineligible|cannot|can't)\b", re.I)  # "graduated in 2025 … are not eligible"
TITLE_EVENT = re.compile(r"\b(?:hiring\s+events?|career\s+fairs?|events?|webinars?|info(?:rmation)?\s+sessions?|"
                         r"open\s+house|req(?:uisition)?)\b", re.I)
CLASS_YEAR = re.compile(r"\bclass\s+of\s+'?(?P<year>20\d\d)\b|\b(?P<year2>20\d\d)\s+(?:new\s+)?grad(?:uate)?s\b", re.I)
# An earliest date or an open end is no deadline: "as early as January 2026", "2026 or later".
OPEN_END = re.compile(r"\bas\s+early\s+as\b|\bno\s+earlier\s+than\b|\bearliest\b|\bafter\b|\bor\s+later\b|"
                      r"\band\s+later\b|\bonwards?\b|\bbeyond\b", re.I)
MAY_DATE = re.compile(r"\bmay\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?'?20\d\d\b", re.I)  # the month, not the hedge
CLASS_TITLE = re.compile(r"\b(?:new\s*grads?|new\s*graduates?|graduates?|grad|university|college|campus|"
                         r"early[\s-]*career|class\s+of|start(?:ing)?|entry[\s-]*level|cohort)\b", re.I)


def _period_end(match) -> tuple[int, int]:
    """The last month a period covers: a month itself, a season's last month, a quarter's, a bare year's December."""
    year = int(match["year"])
    if match["month"]:
        return year, MONTH_NUMBER[match["month"].lower().rstrip(".")]
    if match["season"]:
        season = match["season"].lower()
        return (year + 1, 2) if season == "winter" else (year, SEASON_END[season])
    if match["quarter"]:
        return year, int(match["quarter"][1]) * 3
    return year, 12


def _latest(text: str) -> tuple[int, int] | None:
    ends = [_period_end(m) for m in PERIOD.finditer(text)]
    return max(ends) if ends else None


def start_periods(titles, sentences) -> tuple[tuple[int, int], ...]:
    """Each explicit start date or graduation class found, as the last (year, month) it allows: a title that names a
    class ("2026 New Grad", "Class of 2026", "2026 Start"), or a required sentence that says when the job starts or
    by when candidates graduate. Earliest dates, open ends, hedged and preferred sentences don't count."""
    found = []
    for title in titles:
        if title and CLASS_TITLE.search(title) and not TITLE_EVENT.search(title):
            latest = _latest(title)
            if latest:
                found.append(latest)
    preferred_section = False
    for sentence in sentences:
        sentence = sentence.strip().strip("#*• ")
        if SOFT_HEADING.fullmatch(sentence):
            preferred_section = True
            continue
        if HARD_HEADING.fullmatch(sentence):
            preferred_section = False
            continue
        if not DIGIT.search(sentence):
            continue
        if preferred_section or SOFT.search(sentence) or HEDGE.search(MAY_DATE.sub("DATE", sentence)) \
                or OPEN_END.search(sentence) or NEGATED.search(sentence):
            continue
        items = []
        if not NOT_THE_JOB.search(sentence):
            for match in START_THEN.finditer(sentence):
                before = sentence[:match.start()]
                if re.search(r"\bsince\s+$", before, re.I):
                    continue  # "since starting in 2025": history
                if not before.strip() and re.match(r"\s*(?:in\s+|on\s+)?[^,]{0,30}?\b20\d\d\s*,", match["rest"]):
                    continue  # a leading "Beginning January 1, 2025, …": when a rule or policy applies
                items.append(_latest(match["rest"]))
            for match in THEN_START.finditer(sentence):
                items.append(_period_end(match))
        for match in GRADUATING.finditer(sentence):
            items.append(_latest(match["rest"] or match["rest2"] or ""))
        for match in CLASS_YEAR.finditer(sentence):
            items.append((int(match["year"] or match["year2"]), 12))
        found += [item for item in items if item]
    return tuple(found)


@dataclass(frozen=True)
class Evidence:
    """The slow part of screening, which no rule changes: read once to judge a job under several rule sets."""
    sponsorship: str
    years_required: int | None
    location: str
    start_periods: tuple[tuple[int, int], ...] = ()  # start_periods(): explicit start dates and graduation classes


def evidence(facts: JobFacts) -> Evidence:
    sentences = _sentences(facts.description or "")
    return Evidence(_sponsorship(facts, sentences), _years_required(sentences),
                    job_location(facts.locations, facts.countries), start_periods(facts.titles, sentences))


def screen(facts: JobFacts, rules: ScreeningRules, read: Evidence | None = None) -> Screening:
    """`read` is evidence(facts), passed in when the caller judges the same job under several rule sets."""
    read = evidence(facts) if read is None else read
    title = " / ".join(t for t in facts.titles if t)
    new_grad = bool(NEW_GRAD.search(title))
    reasons = []
    if rules.exclude_internships and _internship(facts, title):
        reasons.append("internship")
    if rules.exclude_senior and not new_grad and any(_senior(t) for t in facts.titles):
        reasons.append("senior_title")
    role = _role(facts, title, rules)
    if rules.exclude_non_cs and role == "non_cs":
        reasons.append("not_cs_role")
    if rules.exclude_unknown_without_software_evidence and role == "unknown" and facts.description.strip() \
            and not SOFTWARE_EVIDENCE.search(facts.description):
        reasons.append("no_software_evidence")
    sponsorship = read.sponsorship
    if rules.exclude_citizenship_required and sponsorship == "citizenship_required":
        reasons.append("citizenship_required")
    years = read.years_required
    if rules.max_years_required is not None and years is not None \
            and years >= rules.max_years_required:
        reasons.append(f"experience_required:{years}")
    if facts.closed_at_source:
        reasons.append("closed_at_source")
    location = read.location
    if rules.us_only and location == "non_us":
        reasons.append("outside_us")
    if rules.graduation:
        year, month = (int(part) for part in rules.graduation.split("-"))
        # Years well before graduation are history ("since starting in 2019"), not this job's dates. With several
        # dates, the latest decides: conflicting evidence keeps the job.
        recent = [p for p in read.start_periods if p[0] >= year - 2]
        if recent and max(recent) < (year, month):
            last = max(recent)
            reasons.append(f"start_before_graduation:{last[0]:04d}-{last[1]:02d}")
    signals = {"role": role, "sponsorship": sponsorship, "years_required": years, "new_grad_title": new_grad,
               "has_description": bool(facts.description.strip()), "location": location}
    return Screening("excluded" if reasons else "kept", tuple(reasons), signals)


def stated_internship(title: str, employment_types: tuple[str, ...] = ()) -> bool:
    """An internship by its own title or employment type, the evidence one posting carries by itself."""
    if any(value.strip().lower() == "internship" for value in employment_types) or INTERN.search(title):
        return True
    return bool(SEASON.search(title)) and not NEW_GRAD.search(title)  # "Software Engineer (Summer 2027)"


def _internship(facts: JobFacts, title: str) -> bool:
    if stated_internship(title, facts.employment_types):
        return True
    return bool(facts.sources) and all(INTERN.search(source.replace("_", " ")) for source in facts.sources)


def _senior(title: str) -> bool:
    # Member of Technical Staff is used for multiple levels, including graduates.
    title = re.sub(r"\bmember\s+of\s+(?:the\s+)?technical\s+staff\b", "engineer", title, flags=re.I)
    # A team/product suffix can contain job-level words without describing this role.
    parts = re.split(r"\s+(?:[-–|])\s+|,\s*", title)
    if SENIOR.search(parts[0]):
        return True
    return any(SENIOR.fullmatch(part.strip(" .()")) or LEVEL_PART.fullmatch(part.strip(" .()")) or
               re.fullmatch(r"(?:iii|iv|v|vi)", part.strip(" .()"), re.I) or
               (SENIOR.search(part) and re.search(r"\b(?:engineer|developer|scientist|architect)\b", part, re.I))
               for part in parts[1:])


@lru_cache(maxsize=32)
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


def _sponsorship(facts: JobFacts, sentences: list[str]) -> str:
    labels = {LABELS.get(label.strip().lower()) for label in facts.sponsorship_labels} - {None}
    required = any(TITLE_CLEARANCE.search(t) for t in facts.titles) or bool(citizenship_evidence(facts.description))
    if "citizenship_required" in labels or required:
        return "citizenship_required"
    # Questions and conditional language supply no affirmative sponsorship evidence.
    concrete = [s for s in sentences if SPONSOR_WORD.search(s) and not re.search(
        r"\?|\b(?:guarantee|might|may|possibly|not\s+unavailable)\b", s, re.I)]
    if any(NO_SPONSOR.search(s) for s in concrete):  # employer words win over repository labels
        return "no"
    if "offers" in labels or any(OFFERS_SPONSOR.search(s) for s in concrete):
        return "offers"
    return "no" if "no" in labels else "unknown"


def _preferred_heading(line: str) -> bool:
    line = line.strip("#*•() ")
    return len(line.split()) <= 10 and bool(PREFERRED_HEADING.match(line)
                                            or (line.endswith(":") and PREFERRED_WORD.search(line)))


def citizenship_evidence(description: str) -> str | None:
    """The first sentence that requires U.S. citizenship, U.S.-person status or a security clearance.

    A preferred section ("Nice to Have", "Ways to stand out") holds list items only: the first line that is not
    a list item ends it, so a later ITAR paragraph is read again.
    """
    text = re.sub(r"\bU\.\s*S\.", "US", description or "", flags=re.I).replace("’", "'")
    heading, preferred, item = "", False, False
    for line in text.splitlines():
        line = line.strip()
        if not line.strip(BULLETS):
            item = item or bool(line)  # a lone "-": the next line is a list item
            continue
        if not (item or line[0] in BULLETS):
            preferred = _preferred_heading(line)
        item = False
        if preferred and not EXPLICIT.search(line):
            continue  # "Secret Clearance required." still counts when an employer lists it under "Preferred"
        if not heading and not CITIZENSHIP_WORD.search(line):
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", line):
            if not heading and not CITIZENSHIP_WORD.search(sentence):
                continue
            if CLEARANCE_HEADING.fullmatch(sentence.strip("#*•- ")):
                heading = sentence + " "  # "Security Clearance" over "Top Secret/SCI required"
                continue
            sentence, heading = heading + sentence, ""
            if "?" in sentence or NEGATED_REQUIREMENT.search(sentence) or SOFT.search(sentence) \
                    or HEDGE.search(sentence):
                continue
            if not NON_EXCLUSIVE.search(sentence) and (
                    CITIZENSHIP.search(sentence) or STATUS_ITEM.fullmatch(sentence.strip(BULLETS).rstrip(".;"))):
                return sentence
            sentence = OTHER_CLEARANCE.sub(" ", sentence)
            if NO_CLEARANCE.search(sentence):
                continue
            named = CLEARANCE.search(sentence)
            if CLEARANCE_VALUE.search(sentence) \
                    or (named and CLEARANCE_CUE.search(sentence) and not PAY.search(sentence[:named.start()])) \
                    or (LEVEL_CLEARANCE.search(sentence) and len(sentence.split()) <= 8):
                return sentence
    return None


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
        elif key == "graduation":
            if value is not None and (not isinstance(value, str)
                                      or not re.fullmatch(r"20\d\d-(?:0[1-9]|1[0-2])", value)):
                raise ValueError(f"Cannot read {where}: graduation must be \"YYYY-MM\" (e.g. \"2027-05\") or null")
            values[key] = value
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
