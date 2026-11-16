"""A folder of OSV records as the advisory register, and an OpenVEX document for the verdicts: the register that
ships with Arceus (integrations.AdvisoryRegister). It needs no service and no secret, and makes no request.

    secops:
      register:
        type: files
        advisories: ~/arceus/data/secops/advisories      # one OSV record per .json file
        verdicts: ~/arceus/data/secops/verdicts.json     # optional: without it the register is read-only
        author: "ACME Security <security@example.com>"   # required with verdicts

Reading follows the OSV schema (ossf/osv-schema): `id`, `modified` as the marker that moves with the content,
`summary` the title, `details` the text, `aliases` the identifiers; a record with `withdrawn` is not open. Severity,
`affected` and `references` become the facts the agent reads, the first ADVISORY or WEB reference the advisory's
page. A file that is not a usable record is given as an advisory without its text, so a run leaves it out and its
report names it, as `arceus-runner check` does.

Writing follows OpenVEX v0.2.0 (openvex/spec): one document, rewritten whole and atomically for each verdict, which
is a new statement on the vulnerability named by the advisory's id for the product that is the repository's address
on the code host. A repository's latest verdict on an advisory is its last statement there.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field, model_validator

from arceus_runner import __version__
from arceus_runner.checks import Check
from arceus_runner.config import AdvisoryRegisterConfig, ExpandedPath, HttpConfig
from arceus_runner.http import ServiceError
from arceus_runner.integrations import Advisory, Id, RegisterType, RepoAt
from arceus_runner.workspace_run import write_atomic

NAME = "files"
MAX_RECORDS = 5000           # files in the folder: more is a mistake, not a register to read part of
MAX_BYTES = 1024 * 1024      # one record
MAX_LIST = 50                # entries of a list in the facts; the rest are counted
MAX_EVIDENCE = 4000
OSV_ID = re.compile(r"^[A-Za-z0-9:_.-]{1,100}$")  # the schema's characters for an id
EVENTS = ("introduced", "fixed", "last_affected", "limit")
CONTEXT = "https://openvex.dev/ns/v0.2.0"
STATUS = {"affected": "affected", "fixed": "fixed", "not_affected": "not_affected",
          "needs_review": "under_investigation"}
WORD = {status: word for word, status in STATUS.items()}
TOOLING = f"Arceus advisory triage (arceus-runner {__version__})"


class FilesConfig(AdvisoryRegisterConfig):
    advisories: ExpandedPath                # the folder of OSV records
    verdicts: ExpandedPath | None = None    # the OpenVEX document; none: read-only, verdicts stay in the reports
    author: str | None = Field(default=None, min_length=1)  # the document's author: a person or an organization

    @model_validator(mode="after")
    def _author(self) -> FilesConfig:
        if self.verdicts and not self.author:
            raise ValueError("author: required with verdicts: who the OpenVEX document says made its statements, "
                             'like "ACME Security <security@example.com>"')
        return self


def _dicts(value: object) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _texts(value: object) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v}


def _capped(items: list[str]) -> list[str]:
    return items[:MAX_LIST] + ([f"and {len(items) - MAX_LIST} more"] if len(items) > MAX_LIST else [])


def _package(a: dict) -> str:
    p = a.get("package") if isinstance(a.get("package"), dict) else {}
    return _words(p.get("ecosystem"), p.get("name")) or _words(p.get("purl"))


def _words(*values: object) -> str:
    return " ".join(v for v in values if isinstance(v, str))


def _range(r: dict) -> str:
    """One version range, readable: `ECOSYSTEM: introduced 0, fixed 2.4.1`."""
    return _words(r.get("type"), r.get("repo")) + ": " + ", ".join(
        f"{k} {v}" for e in _dicts(r.get("events")) for k, v in e.items() if k in EVENTS and isinstance(v, str))


def read_record(path: Path) -> Advisory | None:
    """The OSV record in `path` as an advisory, None when it is withdrawn; ValueError when it is not a usable one."""
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"larger than {MAX_BYTES // 1024} KB")

    rec = json.loads(path.read_bytes())
    if not isinstance(rec, dict):
        raise ValueError("not a JSON object")

    rid, modified = rec.get("id"), rec.get("modified")
    if not isinstance(rid, str) or not OSV_ID.match(rid):
        raise ValueError("no usable `id`")

    if not isinstance(modified, str) or not modified:
        raise ValueError("no `modified`")

    if rec.get("withdrawn"):
        return None

    affected, refs = _dicts(rec.get("affected")), _dicts(rec.get("references"))
    blocks = [rec, *affected]  # severity is the record's, or each package's
    scores = [f"{s['type']} {s['score']}" for b in blocks for s in _dicts(b.get("severity"))
              if isinstance(s.get("type"), str) and isinstance(s.get("score"), str)]
    words = [d["severity"] for b in blocks for d in (b.get("database_specific"), b.get("ecosystem_specific"))
             if isinstance(d, dict) and isinstance(d.get("severity"), str)]
    aliases = _texts(rec.get("aliases"))[:MAX_LIST]
    packages = [_compact({"package": _package(a), "ranges": _capped([_range(r) for r in _dicts(a.get("ranges"))]),
                          "versions": _capped(_texts(a.get("versions")))}) for a in affected[:MAX_LIST]]
    fixed = [f"{_package(a)} {e['fixed']}" for a in affected for r in _dicts(a.get("ranges"))
             for e in _dicts(r.get("events")) if isinstance(e.get("fixed"), str)]
    links = [r for r in refs if isinstance(r.get("url"), str)]
    facts = {"aliases": aliases, "severity": _capped(scores + words), "affected": packages, "fixed": _capped(fixed),
             "references": _capped([_words(r.get("type"), r["url"]) for r in links])}
    summary, details = rec.get("summary"), rec.get("details")

    return Advisory(id=rid, ref=rid, title=summary[:300] if isinstance(summary, str) else "",
                    text=details if isinstance(details, str) else "", facts=_compact(facts),
                    identifiers=aliases, severity=words[0].strip().lower()[:20] if words else "", changed=modified,
                    url=next((r["url"] for r in links if r.get("type") in ("ADVISORY", "WEB")), None))


class FilesRegister:
    max_evidence = MAX_EVIDENCE

    def __init__(self, cfg: FilesConfig):
        self.cfg = cfg
        self.receives = cfg.verdicts is not None

    def open(self) -> list[Advisory]:
        """The folder's open advisories; a file that is not a usable record is one without its text, saying why (its
        id is its path: an OSV id has no `/`)."""
        folder = self.cfg.advisories
        try:
            files = sorted(p for p in folder.iterdir() if p.suffix == ".json")
        except OSError as err:
            raise ServiceError(NAME, f"the advisories folder {folder} cannot be read ({err.strerror or err})") from None

        if len(files) > MAX_RECORDS:
            raise ServiceError(NAME, f"{folder} holds {len(files)} records, more than the {MAX_RECORDS} it reads")

        found: dict[Id, Advisory] = {}
        for path in files:
            try:
                a = read_record(path)
            except (OSError, ValueError, RecursionError) as err:
                why = err if isinstance(err, ValueError) else type(err).__name__
                a = Advisory(id=str(path), ref=path.name, title="", text=None, facts={},
                             problem=f"{path.name} is not a usable OSV record ({why})")
            if a is not None and (a.id not in found or a.changed > found[a.id].changed):
                found[a.id] = a  # OSV: of two records with one id, the later `modified` holds

        return list(found.values())

    def _document(self) -> dict:
        """The OpenVEX document as it is on disk, or a new one."""
        path = self.cfg.verdicts
        try:
            doc = json.loads(path.read_bytes())
        except FileNotFoundError:
            return {"@context": CONTEXT, "@id": f"urn:uuid:{uuid.uuid4()}", "author": self.cfg.author,
                    "timestamp": "", "version": 0, "statements": []}
        except (OSError, ValueError, RecursionError) as err:
            raise ServiceError(NAME, f"the verdicts document {path} cannot be read ({type(err).__name__})") from None

        if not (isinstance(doc, dict) and doc.get("@context") == CONTEXT and isinstance(doc.get("@id"), str)
                and isinstance(doc.get("timestamp"), str) and type(doc.get("version")) is int
                and isinstance(doc.get("statements"), list) and all(isinstance(s, dict) for s in doc["statements"])):
            raise ServiceError(NAME, f"the verdicts document {path} is not an OpenVEX v0.2.0 document")

        return doc

    def send(self, advisory: Advisory, repo: RepoAt, verdict: str, evidence: str) -> None:
        doc = self._document()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        statement = {"vulnerability": _compact({"name": str(advisory.id), "aliases": advisory.identifiers}),
                     "timestamp": now, "products": [{"@id": repo.url}], "status": STATUS[verdict],
                     "status_notes": f"Read at commit {repo.commit} of {repo.path}.\n\n{evidence}"}

        if verdict == "not_affected":  # the specification wants a justification or this
            statement["impact_statement"] = evidence
        elif verdict == "affected":  # and here what to do about it
            fixed = advisory.facts.get("fixed") or []  # read_record's own facts
            statement["action_statement"] = (f"Update to a fixed version: {', '.join(fixed)}." if fixed else "The "
                                             f"advisory names no fixed version: see {advisory.url or advisory.ref}.")
        for s in doc["statements"]:
            s.setdefault("timestamp", doc["timestamp"])  # what a statement inherited stays its own (OpenVEX: updating)

        doc["statements"].append(statement)
        doc.update(author=self.cfg.author, timestamp=now, last_updated=now, version=doc["version"] + 1,
                   tooling=TOOLING)
        try:
            write_atomic(self.cfg.verdicts, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        except (OSError, UnicodeError) as err:  # also a text made elsewhere that UTF-8 cannot hold, never quoted
            why = (err.strerror or err) if isinstance(err, OSError) else type(err).__name__
            raise ServiceError(NAME, f"the verdicts document {self.cfg.verdicts} cannot be written ({why})") from None

    def latest(self, advisory: Id, repo: RepoAt) -> tuple[str, str] | None:
        last = None

        for s in self._document()["statements"]:
            v, status = s.get("vulnerability"), s.get("status")
            if (
                isinstance(v, dict) and
                v.get("name") == str(advisory) and
                isinstance(status, str) and
                status in WORD and
                any(p.get("@id") == repo.url for p in _dicts(s.get("products")))
            ):
                notes = s.get("status_notes")
                last = (WORD[status], notes.partition("\n\n")[2] if isinstance(notes, str) else "")

        return last

    def check(self, scope: str, post: bool) -> list[Check]:
        """The folder and its records; with `post`, the document and whether it can be written. A folder of the
        project's own covers the project whatever its group."""
        found = self.open()
        unusable = [a.problem for a in found if a.text is None]
        n = len(found) - len(unusable)
        out = [Check("files.advisories", "ok", f"{n} open advisor{'ies' if n != 1 else 'y'} in {self.cfg.advisories}")]
        if unusable:
            out.append(Check("files.records", "warn", f"{len(unusable)} file(s) a run leaves out and names: "
                                                      + "; ".join(unusable[:10])))
        if post and self.receives:
            path = self.cfg.verdicts
            try:
                held = len(self._document()["statements"])
            except ServiceError as err:
                out.append(Check("files.verdicts", "fail", str(err)))
            else:
                writable = os.access(path.parent, os.W_OK)
                out.append(Check("files.verdicts", "ok" if writable else "fail",
                                 f"{path}: {held} statements" if writable else f"{path.parent} cannot be written to"))
        return out

    def close(self) -> None:
        pass


def _build(cfg: FilesConfig, token: str | None, http: HttpConfig, transport=None) -> FilesRegister:
    return FilesRegister(cfg)


REGISTER = RegisterType(config=FilesConfig, build=_build)
