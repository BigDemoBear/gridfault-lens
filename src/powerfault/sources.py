"""Read-only inputs and reproducible snapshots. No archive extraction to user paths."""
from __future__ import annotations
import codecs
from pathlib import Path, PurePosixPath
import shutil
import zipfile
import xml.etree.ElementTree as ET
from .model import Source, Issue, CaseError

TEXT = {".cfg", ".hdr", ".inf", ".xml"}
SUPPORTED = TEXT | {".dat"}
EXCLUDED = {".doc", ".docx", ".pdf", ".rpt", ".txt", ".md", ".rtf", ".html", ".htm", ".csv", ".json", ".odt", ".xls", ".xlsx", ".ppt", ".pptx"}
IGNORED = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
MAX_TEXT = 32 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024

def decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-16"
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise CaseError("Text is neither UTF-8, BOM UTF-16 nor GB18030; supply a decoded text copy")

def canonical_text(raw: Path, suffix: str) -> tuple[str | None, str | None, str | None]:
    if suffix == ".dat":
        return None, None, None
    if suffix not in TEXT:
        raise CaseError("Only native recording files can supply text evidence")
    text, enc = decode_text(raw.read_bytes())
    # Offsets always address this frozen Unicode snapshot, never decoded byte positions.
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None

def collect(case: Path, output: Path) -> tuple[list[Source], list[Issue]]:
    case, output = case.resolve(), output.resolve()
    archive_input = case.is_file() and case.suffix.lower() == ".zip"
    if not case.is_dir() and not archive_input:
        raise CaseError("Case must be a directory or ZIP archive")
    if output == case or case in output.parents:
        raise CaseError("Output must be outside the input case directory")
    if output.exists() and any(output.iterdir()):
        raise CaseError("Output directory must be new or empty; runs are immutable")
    output.mkdir(parents=True, exist_ok=True)
    (output / "raw").mkdir(exist_ok=True)
    (output / "texts").mkdir(exist_ok=True)
    sources, issues = [], []

    def add(label: str, suffix: str, size: int, copy) -> None:
        sid = "s" + str(len(sources) + 1).zfill(5)
        raw = output / "raw" / (sid + suffix)
        source = Source(sid, label, suffix.lstrip("."), raw.relative_to(output).as_posix(), size)
        sources.append(source)
        if size > (MAX_BINARY if suffix == ".dat" else MAX_TEXT):
            source.problem = "SIZE_LIMIT: file retained in manifest but not processed"
        else:
            try:
                copy(raw)
                source.text, source.encoding, source.problem = canonical_text(raw, suffix)
            except (OSError, UnicodeError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
                source.problem = type(exc).__name__ + ": " + str(exc)
        if source.text is not None:
            try:
                source.text.encode("utf-8")
            except UnicodeError as exc:
                source.text = None
                source.problem = "TEXT_ENCODING_ERROR: " + str(exc)
            else:
                (output / "texts" / (sid + ".txt")).write_text(source.text, encoding="utf-8")
        if source.problem:
            issues.append(Issue("SOURCE_UNREAD", source.problem, sid))

    paths = [case] if archive_input else sorted(case.rglob("*"), key=lambda x: x.as_posix().lower())
    for p in paths:
        if not p.is_file() or p.name.startswith("~$"):
            continue
        label, suffix = (p.name if archive_input else p.relative_to(case).as_posix()), p.suffix.lower()
        if not p.resolve().is_relative_to(case):
            issues.append(Issue("EXTERNAL_SYMLINK", label))
            continue
        if suffix in SUPPORTED:
            add(label, suffix, p.stat().st_size, lambda target, p=p: shutil.copyfile(p, target))
        elif suffix == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for entry in sorted(z.infolist(), key=lambda x: x.filename.lower()):
                        if entry.is_dir():
                            continue
                        name = PurePosixPath(entry.filename.replace("\\", "/"))
                        if name.is_absolute() or ".." in name.parts or ":" in str(name):
                            issues.append(Issue("UNSAFE_ARCHIVE_MEMBER", label + "!" + entry.filename))
                            continue
                        ext = name.suffix.lower()
                        if ext in SUPPORTED:
                            def copy_member(target, entry=entry):
                                with z.open(entry) as src, target.open("wb") as dst:
                                    shutil.copyfileobj(src, dst)
                            add(label + "!" + name.as_posix(), ext, entry.file_size, copy_member)
                        elif ext in EXCLUDED:
                            issues.append(Issue("EXCLUDED_NON_RECORDING", label + "!" + str(name)))
                        elif ext not in IGNORED:
                            issues.append(Issue("UNSUPPORTED_ARCHIVE_MEMBER", label + "!" + str(name)))
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                issues.append(Issue("ARCHIVE_UNREAD", label + ": " + str(exc)))
        elif suffix in EXCLUDED:
            issues.append(Issue("EXCLUDED_NON_RECORDING", label))
        elif suffix not in IGNORED:
            issues.append(Issue("UNSUPPORTED_FILE", label))
    if not sources:
        raise CaseError("No supported source files found")
    return sources, issues

def restore(run: Path, manifest: dict) -> list[Source]:
    sources = []
    for item in manifest["sources"]:
        s = Source(**item)
        if s.text is not None:
            # Manifest stores only the address; the canonical text is a separate frozen file.
            s.text = (run / "texts" / (s.id + ".txt")).read_text(encoding="utf-8")
        sources.append(s)
    return sources
