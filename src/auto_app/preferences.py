"""Small, deterministic job preferences shared by queries and model selection.

JSON keeps the agent's read-only script runnable with Python's standard library.
These exclusions are a view over saved evidence, never a manual job decision.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata


def _text(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c))


PROVINCES = re.compile(r"\b(?:alberta|british columbia|manitoba|new brunswick|newfoundland(?: and labrador)?|"
                       r"nova scotia|northwest territories|nunavut|ontario|prince edward island|quebec|"
                       r"saskatchewan|yukon)\b")
PROVINCE_CODES = re.compile(r"(?:^|[,;/])\s*(?:ab|bc|mb|nb|nl|ns|nt|nu|on|pe|qc|sk|yt)\s*(?:$|[,;/])")
US_LOCATION = re.compile(
    r"(?<!\w)(?:united states(?: of america)?|usa|u\.s\.(?:a\.)?)(?!\w)|"
    r"(?:^|[,;/])\s*(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|"
    r"mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc|us|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
    r"idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
    r"minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
    r"north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|"
    r"tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia)"
    r"\s*(?:$|[,;/])"
)


def _canada(location: str) -> bool:
    text = _text(location)
    if re.search(r"\b(?:canada|canadian)\b", text):
        return True
    if PROVINCE_CODES.search(text):
        return True
    # US cities can share province names (New Brunswick, NJ; Ontario, CA).
    # An explicit Canadian country still wins; otherwise a US state disambiguates.
    if US_LOCATION.search(text):
        return False
    return bool(PROVINCES.search(text))


@dataclass(frozen=True)
class Preferences:
    exclude_countries: tuple[str, ...] = ()

    def countries_for(self, location: str) -> list[str]:
        text = _text(location)
        return [country for country in self.exclude_countries
                if (_canada(location) if country == "Canada" else
                    re.search(r"(?<!\w)" + re.escape(_text(country)) + r"(?!\w)", text))]

    def evaluate(self, locations: list[str]) -> dict:
        matched, countries = [], set()
        for location in dict.fromkeys(locations):
            excluded = self.countries_for(location)
            if excluded:
                matched.append(location)
                countries.update(excluded)
        return {"excluded": bool(countries), "reasons": [f"excluded_country:{c}" for c in sorted(countries)],
                "matched_locations": matched}


def load_preferences(path: Path, *, required: bool = False) -> Preferences:
    if not required and not path.exists():
        return Preferences()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) - {"exclude_countries"}:
            raise ValueError("expected an object with exclude_countries")
        countries = data.get("exclude_countries", [])
        if not isinstance(countries, list) or any(not isinstance(c, str) or not c.strip() for c in countries):
            raise ValueError("exclude_countries must be a list of country names")
        canonical = ["Canada" if c.strip().casefold() in {"canada", "ca", "can"} else c.strip() for c in countries]
        return Preferences(tuple(dict.fromkeys(canonical)))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read preferences {path}: {exc}") from exc


def preferences_for_database(db_path: Path) -> Path:
    home = db_path.parent.parent if db_path.parent.name == "data" else db_path.parent
    return home / "config" / "preferences.json"
