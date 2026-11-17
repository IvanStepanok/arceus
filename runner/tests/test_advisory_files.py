"""The built-in advisory register: OSV records read from a folder, verdicts written as one OpenVEX document."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from arceus_runner import advisory_files, workspace_run
from arceus_runner.advisory_files import FilesRegister
from arceus_runner.config import Config, load_secrets
from arceus_runner.http import ServiceError
from arceus_runner.integrations import Advisory, RepoAt, build_register

from .conftest import GL_TOKEN, YT_TOKEN, example_raw

REPO = RepoAt("acme-platform", "clients/acme", "https://git.example.com/clients/acme/acme-platform.git", "a1b2c3d")
IMAGE = {  # a PyPI package with a fixed version and every field the register reads
    "schema_version": "1.7.0", "id": "x_ACME-2099-1", "modified": "2026-11-02T08:00:00Z",
    "aliases": ["CVE-2099-1011"], "summary": "Heap overflow in acme-imagelib's PNG decoder",
    "details": "Services that decode untrusted PNG files with acme-imagelib before 2.4.1.",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    "affected": [{"package": {"ecosystem": "PyPI", "name": "acme-imagelib"}, "versions": ["2.3.0", "2.4.0"],
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.4.1"}]}]}],
    "references": [{"type": "FIX", "url": "https://code.example.com/acme-imagelib/commit/1"},
                   {"type": "ADVISORY", "url": "https://advisories.example.com/x_ACME-2099-1"}],
    "database_specific": {"severity": "HIGH"}}
LEFTPAD = {  # an npm package with no fix yet
    "id": "x_ACME-2099-2", "modified": "2026-11-03T08:00:00Z", "summary": "Prototype pollution in left-pad-ng",
    "affected": [{"package": {"ecosystem": "npm", "name": "left-pad-ng"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "1.0.0"}]}]}]}


def folder(tmp_path: Path, **files) -> Path:
    path = tmp_path / "advisories"
    path.mkdir(exist_ok=True)
    for name, content in files.items():
        (path / name).write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def register(tmp_path: Path, verdicts: bool = True, **files) -> FilesRegister:
    (tmp_path / "vex").mkdir(exist_ok=True)
    return FilesRegister(advisory_files.FilesConfig(
        type="files", advisories=folder(tmp_path, **{"acme-imagelib.json": IMAGE, "left-pad.json": LEFTPAD, **files}),
        verdicts=tmp_path / "vex" / "verdicts.json" if verdicts else None, author="ACME Security"))


def document(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "vex" / "verdicts.json").read_text())


def test_the_open_advisories_are_the_records_of_the_folder_read_by_the_osv_schema(tmp_path):
    r = register(tmp_path, **{
        "withdrawn.json": {**LEFTPAD, "id": "x_ACME-2099-3", "withdrawn": "2026-11-04T00:00:00Z"},
        "older.json": {**IMAGE, "modified": "2026-11-01T08:00:00Z", "summary": "an older copy"},
        "broken.json": "{", "list.json": [IMAGE], "no-date.json": {"id": "x_ACME-2099-4"},
        "slash.json": {**LEFTPAD, "id": "x/ACME"}, "number.json": {**LEFTPAD, "id": 2099}, "README.md": "not a record"})
    found = r.open()
    assert [a for a in found if a.text is not None] == [Advisory(
        id="x_ACME-2099-1", ref="x_ACME-2099-1", title="Heap overflow in acme-imagelib's PNG decoder",
        text="Services that decode untrusted PNG files with acme-imagelib before 2.4.1.", identifiers=["CVE-2099-1011"],
        severity="high", changed="2026-11-02T08:00:00Z", url="https://advisories.example.com/x_ACME-2099-1",
        facts={"aliases": ["CVE-2099-1011"],
               "severity": ["CVSS_V3 CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "HIGH"],
               "affected": [{"package": "PyPI acme-imagelib", "ranges": ["ECOSYSTEM: introduced 0, fixed 2.4.1"],
                             "versions": ["2.3.0", "2.4.0"]}],
               "fixed": ["PyPI acme-imagelib 2.4.1"],
               "references": ["FIX https://code.example.com/acme-imagelib/commit/1",
                              "ADVISORY https://advisories.example.com/x_ACME-2099-1"]}),
        Advisory(id="x_ACME-2099-2", ref="x_ACME-2099-2", title="Prototype pollution in left-pad-ng", text="",
                 changed="2026-11-03T08:00:00Z",
                 facts={"affected": [{"package": "npm left-pad-ng", "ranges": ["SEMVER: introduced 1.0.0"]}]})]
    # A file that is not a usable record is an advisory without its text: a run leaves it out and says why.
    unusable = {a.ref: (a.id, a.problem) for a in found if a.text is None}
    where = tmp_path / "advisories"
    assert unusable == {name: (str(where / name), f"{name} is not a usable OSV record ({why})") for name, why in (
        ("broken.json", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
        ("list.json", "not a JSON object"), ("no-date.json", "no `modified`"), ("slash.json", "no usable `id`"),
        ("number.json", "no usable `id`"))}  # an OSV id is a string
    [records] = [c for c in r.check("clients/acme", post=False) if c.name == "files.records"]
    assert records.status == "warn" and records.detail.startswith(
        "5 file(s) a run leaves out and names: broken.json is not a usable OSV record")


def test_the_folder_is_read_within_bounds(tmp_path, monkeypatch):
    many = {**LEFTPAD, "affected": [{"package": {"ecosystem": "npm", "name": "left-pad-ng"},
                                     "versions": [f"1.0.{n}" for n in range(60)]}]}
    versions = register(tmp_path, verdicts=False, **{"left-pad.json": many}).open()[1].facts["affected"][0]["versions"]
    assert len(versions) == advisory_files.MAX_LIST + 1 and versions[-2:] == ["1.0.49", "and 10 more"]
    monkeypatch.setattr(advisory_files, "MAX_BYTES", 600)
    r = register(tmp_path, verdicts=False)
    large, small = r.open()  # the larger record is not read
    assert (large.text, small.id) == (None, "x_ACME-2099-2") and large.problem.endswith("(larger than 0 KB)")
    monkeypatch.setattr(advisory_files, "MAX_RECORDS", 1)
    with pytest.raises(ServiceError, match="holds 2 records, more than the 1 it reads"):
        r.open()  # never a part of the register taken for all of it
    with pytest.raises(ServiceError, match="cannot be read"):
        FilesRegister(r.cfg.model_copy(update={"advisories": tmp_path / "gone"})).open()


def test_each_verdict_is_a_statement_with_what_openvex_requires_of_its_status(tmp_path):
    r = register(tmp_path)
    image, leftpad = r.open()
    for advisory, word in ((image, "affected"), (leftpad, "affected"), (image, "not_affected"),
                           (image, "fixed"), (image, "needs_review")):
        r.send(advisory, REPO, word, f"{word}: `requirements.txt:3` pins it")
    doc = document(tmp_path)
    assert doc["@context"] == "https://openvex.dev/ns/v0.2.0" and doc["@id"].startswith("urn:uuid:")
    assert (doc["author"], doc["version"], doc["tooling"]) == ("ACME Security", 5, advisory_files.TOOLING)
    assert doc["timestamp"] == doc["last_updated"] == doc["statements"][-1]["timestamp"]
    first, second, unaffected, fixed, review = doc["statements"]
    assert first["vulnerability"] == {"name": "x_ACME-2099-1", "aliases": ["CVE-2099-1011"]}
    assert first["products"] == [{"@id": REPO.url}] and first["status"] == "affected"
    assert first["status_notes"] == "Read at commit a1b2c3d of acme-platform.\n\naffected: `requirements.txt:3` pins it"
    assert first["action_statement"] == "Update to a fixed version: PyPI acme-imagelib 2.4.1."
    assert second["vulnerability"] == {"name": "x_ACME-2099-2"}
    assert second["action_statement"] == "The advisory names no fixed version: see x_ACME-2099-2."
    assert unaffected["impact_statement"] == "not_affected: `requirements.txt:3` pins it"
    assert (fixed["status"], review["status"]) == ("fixed", "under_investigation")
    for s in doc["statements"]:  # the four data points of a complete statement, and nothing a status may not carry
        assert {"vulnerability", "products", "status", "timestamp"} <= set(s)
        assert ("impact_statement" in s) == (s["status"] == "not_affected")
        assert ("action_statement" in s) == (s["status"] == "affected")


def test_a_repositorys_latest_verdict_is_its_last_statement_on_the_advisory(tmp_path):
    r = register(tmp_path)
    image, leftpad = r.open()
    assert r.latest(image.id, REPO) is None  # no document yet
    r.send(image, REPO, "affected", "pinned at `2.3.0`")
    r.send(image, REPO, "needs_review", "the version is set at deploy time")
    r.send(image, RepoAt("acme-theme", "clients/acme", "https://git.example.com/theme.git", "d4e5f6"), "fixed", "x")
    r.send(leftpad, REPO, "not_affected", "no `left-pad-ng` in `package-lock.json`")
    assert r.latest("x_ACME-2099-1", REPO) == ("needs_review", "the version is set at deploy time")
    assert r.latest("x_ACME-2099-2", REPO) == ("not_affected", "no `left-pad-ng` in `package-lock.json`")
    assert r.latest("x_ACME-2099-9", REPO) is None


def test_the_document_is_replaced_whole_and_what_was_in_it_stays(tmp_path, monkeypatch):
    r = register(tmp_path)
    image, _ = r.open()
    path = tmp_path / "vex" / "verdicts.json"
    path.write_text(json.dumps({  # made elsewhere: its statement takes its time from the document
        "@context": "https://openvex.dev/ns/v0.2.0", "@id": "https://vex.example.com/acme", "author": "j.doe",
        "timestamp": "2026-10-01T09:00:00Z", "version": 7,
        "statements": [{"vulnerability": {"name": "CVE-2099-0007"}, "products": [{"@id": REPO.url}],
                        "status": "fixed"}]}))
    r.send(image, REPO, "affected", "pinned at `2.3.0`")
    doc = document(tmp_path)
    assert (doc["@id"], doc["version"], doc["author"]) == ("https://vex.example.com/acme", 8, "ACME Security")
    assert doc["statements"][0]["timestamp"] == "2026-10-01T09:00:00Z" != doc["timestamp"]
    before = path.read_bytes()

    def no_space(*_):
        raise OSError(28, "No space left")
    monkeypatch.setattr(workspace_run.os, "replace", no_space)
    with pytest.raises(ServiceError, match="cannot be written \\(No space left\\)"):
        r.send(image, REPO, "fixed", "updated to `2.4.1`")
    assert path.read_bytes() == before  # all of the new one, or the old one as it was
    monkeypatch.undo()
    path.write_text('{"statements": []}')
    with pytest.raises(ServiceError, match="is not an OpenVEX v0.2.0 document"):
        r.send(image, REPO, "fixed", "updated to `2.4.1`")
    assert path.read_text() == '{"statements": []}'  # never written over
    path.write_bytes(odd := before.replace(b'"CVE-2099-0007"', b'"CVE-2099-0007 \\ud83d"'))  # UTF-8 cannot hold it
    with pytest.raises(ServiceError, match="cannot be written \\(UnicodeEncodeError\\)"):
        r.send(image, REPO, "fixed", "updated to `2.4.1`")
    assert path.read_bytes() == odd


def test_without_verdicts_it_is_read_only_and_the_section_is_the_providers(tmp_path):
    raw = example_raw()
    raw.pop("slack")
    raw["secops"] = {"enabled": True, "register": {"type": "files", "advisories": str(folder(tmp_path)),
                                                   "verdicts": str(tmp_path / "verdicts.json")}}
    with pytest.raises(ValidationError, match="author: required with verdicts"):
        Config.model_validate(raw)
    raw["secops"]["register"] = {"type": "files", "advisories": str(folder(tmp_path)), "url": "https://x.example"}
    with pytest.raises(ValidationError, match="secops.register.url\n  Extra inputs"):
        Config.model_validate(raw)
    del raw["secops"]["register"]["url"]
    cfg = Config.model_validate({**raw, "paths": {"secrets": str(tmp_path / "secrets.env")}})
    secrets = load_secrets(cfg, {"YOUTRACK_TOKEN": YT_TOKEN, "GITLAB_TOKEN": GL_TOKEN})  # it needs no secret
    r = build_register(cfg, secrets)
    assert isinstance(r, FilesRegister) and not r.receives and secrets.register is None
    assert [c.name for c in r.check("clients/acme", post=True)] == ["files.advisories"]


def test_check_says_whether_the_document_can_be_written(tmp_path):
    r = register(tmp_path)
    (tmp_path / "vex").rmdir()
    [_, verdicts] = r.check("clients/acme", post=True)
    assert verdicts.status == "fail" and verdicts.detail.endswith("vex cannot be written to")
    (tmp_path / "vex").mkdir()
    [_, verdicts] = r.check("clients/acme", post=True)
    assert verdicts.status == "ok" and verdicts.detail.endswith("verdicts.json: 0 statements")
    for text, said in (("{", "cannot be read (JSONDecodeError)"),
                       ('{"statements": []}', "is not an OpenVEX v0.2.0 document")):
        (tmp_path / "vex" / "verdicts.json").write_text(text)
        [_, verdicts] = r.check("clients/acme", post=True)
        assert verdicts.status == "fail" and verdicts.detail.endswith(said)
