"""apply-kit/documents/: the user's own files a form may ask for (an unofficial transcript, say), read with no token."""

import json

import pytest

from auto_app.handoff.service import HandoffError
from tests.handoff.test_requests import tmp_path  # noqa: F401  (the runtime one level down, so apply-kit is per test)
from tests.handoff.test_service import env, service  # noqa: F401  (env is a fixture)

PDF = b"%PDF-1.7 fictional transcript\n"


def documents(home):
    folder = home.parent / "apply-kit" / "documents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "neu-unofficial-transcript.pdf").write_bytes(PDF)
    (folder / "linked.pdf").symlink_to(folder / "neu-unofficial-transcript.pdf")  # never served
    (folder / "has space.pdf").write_bytes(PDF)  # not a document name: left out
    (folder / "sub").mkdir()
    return folder


def test_the_list_and_one_document(env):  # noqa: F811
    home, _ = env
    documents(home)
    s = service(home)
    listed = s.documents()["documents"]
    assert [d["name"] for d in listed] == ["neu-unofficial-transcript.pdf"]
    assert listed[0]["bytes"] == len(PDF) and len(listed[0]["sha256"]) == 64
    assert s.document("neu-unofficial-transcript.pdf") == PDF


def test_no_folder_is_an_empty_list(env):  # noqa: F811
    home, _ = env
    assert service(home).documents() == {"documents": []}


@pytest.mark.parametrize("name", ["../answers.md", "linked.pdf", "has space.pdf", "sub", ".hidden", "missing.pdf", ""])
def test_anything_else_is_refused(env, name):  # noqa: F811
    home, _ = env
    documents(home)
    with pytest.raises(HandoffError):
        service(home).document(name)
