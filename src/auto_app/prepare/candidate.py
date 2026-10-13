"""A versioned test fixture, independent of private profile/configuration files."""

from hashlib import sha256
import json
from pathlib import Path


CANDIDATE_ID = "fictional-sales-v1"
RESUME_SHA256 = "37d6949cbb0f4490397180fe65394179cef230d0fbf590c7072d9cff844ed5ed"


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fictional_candidate() -> dict:
    """Fresh dictionary each call; file paths never enter the portable package hash."""
    resume = Path(__file__).resolve().parent / "fixtures" / "fictional-sales-resume.pdf"
    if sha256(resume.read_bytes()).hexdigest() != RESUME_SHA256:
        raise ValueError("Packaged fictional candidate resume failed its integrity check")
    profile = {
        "name": {"first": "Alex TEST ONLY", "last": "Sandbox FICTIONAL", "preferred": None},
        "email": "alex.sandbox@example.invalid", "phone": "+12025550107",
        "location": {"city": "Seattle", "state": "WA", "country": "United States"},
        "links": {"linkedin": "https://www.linkedin.com/in/alex-sandbox-test-only-fictional-candidate-20261011"},
    }
    answers = {
        "legal_name": {"question": "Legal Name", "answer": "Alex TEST ONLY Sandbox FICTIONAL",
                       "kind": "fact", "adapt": False},
        "working_location": {"question": "Working location", "answer": "Seattle, WA, United States",
                             "kind": "fact", "adapt": False},
        "sms_consent": {"question": "Do you consent to receive SMS messages?", "answer": "No",
                        "kind": "fact", "adapt": False},
    }
    package = {"candidate_id": CANDIDATE_ID, "test_only": True, "profile": profile, "answers": answers,
               "resume_sha256": RESUME_SHA256}
    package_hash = sha256(canonical_json(package).encode()).hexdigest()
    profile["cv"] = str(resume)
    return {**package, "resume_path": str(resume), "package_hash": package_hash}


def validate_candidate(candidate: dict) -> None:
    """Only the packaged candidate may be requested, including its source file."""
    if candidate != fictional_candidate():
        raise ValueError("Only the immutable fictional-sales-v1 candidate is allowed")


def validate_run_candidate(candidate: dict) -> None:
    """Verify a per-run copy before browser launch; caller constrains its directory.

    Validate all public facts before opening the attachment, so a substituted
    profile cannot turn this helper into an arbitrary file reader.
    """
    expected = fictional_candidate()
    if not isinstance(candidate, dict) or not isinstance(candidate.get("profile"), dict):
        raise ValueError("Invalid fictional candidate package")
    resume_path = candidate.get("resume_path")
    if (not isinstance(resume_path, str) or candidate["profile"].get("cv") != resume_path
            or Path(resume_path).name != "fictional-sales-resume.pdf"):
        raise ValueError("Invalid fictional candidate resume path")
    normalized = {**candidate, "resume_path": expected["resume_path"],
                  "profile": {**candidate["profile"], "cv": expected["profile"]["cv"]}}
    if normalized != expected:
        raise ValueError("Only the immutable fictional-sales-v1 candidate is allowed")
    resume = Path(resume_path)
    try:
        if resume.is_symlink() or sha256(resume.read_bytes()).hexdigest() != RESUME_SHA256:
            raise ValueError("Fictional candidate resume failed its integrity check")
    except OSError as exc:
        raise ValueError("Fictional candidate resume cannot be read") from exc
