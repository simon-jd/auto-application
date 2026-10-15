"""Is a job in the US? Explicit evidence only (docs/linux-worker.md §2, `us_only`).

A location is "us" when it names the US, a US state or a common US city alias; "non_us" when it names
another country, region or a well-known foreign city (and nothing in the US); otherwise "unknown" —
"Remote" alone, "Hybrid", a city we don't know. Within one place, an explicit US country wins
("Remote - US or Canada"), then Canadian evidence ("Toronto, ON, CA"), then a US state or city
("Dublin, CA"); a string listing several places ("Toronto, ON / Boston, MA") is US if any one is. Across a job's locations any US location keeps it; it is outside the US
only when its explicit places are all abroad.
"""

import re

from auto_app.preferences import US_LOCATION, _canada, _text

US_TOKEN = re.compile(r"(?<![a-z])(?:us|u\.s|usa)(?![a-z])")
US_CITIES = re.compile(
    r"\b(?:nyc|sf|sfo|bay area|silicon valley|new york( city)?|san francisco|los angeles|seattle|boston|chicago|"
    r"austin|denver|atlanta|san diego|san jose|palo alto|mountain view|sunnyvale|menlo park|redmond|"
    r"washington dc)\b")
NON_US = re.compile(
    r"\b(?:canada|mexico|brazil|argentina|chile|colombia|peru|costa rica|uruguay|"
    r"united kingdom|uk|great britain|england|scotland|wales|ireland|germany|france|netherlands|belgium|"
    r"luxembourg|switzerland|austria|spain|portugal|italy|greece|poland|czech(?: republic|ia)?|slovakia|hungary|"
    r"romania|bulgaria|serbia|croatia|slovenia|ukraine|estonia|latvia|lithuania|sweden|norway|denmark|finland|"
    r"iceland|turkey|israel|egypt|morocco|nigeria|kenya|south africa|united arab emirates|uae|saudi arabia|"
    r"qatar|india|pakistan|bangladesh|sri lanka|nepal|china|hong kong|taiwan|japan|south korea|korea|"
    r"singapore|malaysia|indonesia|thailand|vietnam|philippines|australia|new zealand|"
    r"emea|apac|latam|europe|asia|eu|"
    r"london|manchester|edinburgh|dublin|berlin|munich|hamburg|frankfurt|paris|amsterdam|zurich|geneva|"
    r"madrid|barcelona|lisbon|milan|warsaw|krakow|prague|budapest|bucharest|stockholm|copenhagen|oslo|"
    r"helsinki|tel aviv|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|new delhi|gurgaon|gurugram|noida|"
    r"chennai|shanghai|beijing|shenzhen|taipei|tokyo|seoul|sydney|melbourne|toronto|vancouver|montreal|"
    r"ottawa|mexico city|sao paulo|bogota|buenos aires)\b")


US_COUNTRY = re.compile(r"(?<!\w)(?:united states(?: of america)?|usa|u\.s\.(?:a\.)?)(?!\w)")


def _segment(text: str) -> str:
    value = _text(text).strip()
    if not value:
        return "unknown"
    # An explicit US country wins ("Remote - US or Canada"). Otherwise a Canadian place wins over a
    # two-letter state lookalike: in "Toronto, ON, CA" the CA is Canada, not California.
    if US_COUNTRY.search(value) or US_TOKEN.search(value):
        return "us"
    if _canada(text):
        return "non_us"
    if US_LOCATION.search(value) or US_CITIES.search(value):
        return "us"
    return "non_us" if NON_US.search(value) else "unknown"


def classify_location(text: str) -> str:
    """us | non_us | unknown, for one location string; "/" and ";" separate places within it."""
    kinds = {_segment(segment) for segment in re.split(r"[;/]", text or "")}
    if "us" in kinds:
        return "us"
    return "non_us" if "non_us" in kinds else "unknown"


def job_location(locations, countries=()) -> str:
    """us | non_us | unknown for a job: any US location keeps it; abroad only when every explicit place is."""
    kinds = {classify_location(location) for location in locations}
    for country in countries:
        kinds.add("us" if country.strip().upper() == "US" else "non_us")
    if "us" in kinds:
        return "us"
    return "non_us" if "non_us" in kinds else "unknown"
