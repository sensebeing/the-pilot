"""Zero-dependency packaging for The Pilot.

This module is the single owner of which checkout files belong to the package,
how their tree digest is computed, and how PEP 517/660 distributions are
produced. It reads only ``pyproject.toml``, ``README.md``, ``LICENSE`` and the
controlled files under ``src/the_pilot``; it never reads or writes ``build/``,
``dist/`` or ``*.egg-info`` in the checkout, so ignored staging residue cannot
reach a wheel or make an installed tree mismatch its source.

Only the standard library may be imported here: the build backend is imported
from ``backend-path = ["src/the_pilot"]`` before ``the_pilot/__init__.py`` runs
its checkout activation, and this module must stay importable in a bare
``requires = []`` build environment.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import tomllib
import zipfile

PACKAGE_NAME = "the_pilot"
WHEEL_GENERATOR = "the-pilot-packaging"
FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
SOURCE_DOCUMENTS = ("pyproject.toml", "README.md", "LICENSE")
WHEEL_FILE_TEXT = (
    "Wheel-Version: 1.0\n"
    f"Generator: {WHEEL_GENERATOR}\n"
    "Root-Is-Purelib: true\n"
    "Tag: py3-none-any\n"
)


def is_package_file(relative: PurePosixPath) -> bool:
    """Return whether a package-relative path belongs to the installed package.

    This predicate is the single executable owner of the controlled-file
    classification: the source digest, the wheel digest and every built
    distribution call it, so their file sets can never diverge.
    """
    if "__pycache__" in relative.parts or relative.suffix in EXCLUDED_SUFFIXES:
        return False
    return relative.suffix == ".py" or relative.parts[:1] == ("assets",)


def iter_package_files(src_root: Path) -> list[tuple[str, Path]]:
    """List the controlled package files in stable relative-path order.

    Only ordinary files accepted by :func:`is_package_file` belong to the
    package, so the same enumeration drives the digest and every wheel.
    """
    if not src_root.is_dir():
        return []
    files: list[tuple[str, Path]] = []
    for path in src_root.rglob("*"):
        if not path.is_file():
            continue
        relative = PurePosixPath(path.relative_to(src_root).as_posix())
        if is_package_file(relative):
            files.append((relative.as_posix(), path))
    return sorted(files)


def _tree_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def compute_source_tree_digest(src_root: Path) -> str:
    """Hash a source package tree exactly as its installed counterpart is hashed."""
    return _tree_digest(
        [(relative, path.read_bytes()) for relative, path in iter_package_files(src_root)]
    )


def compute_wheel_tree_digest(wheel_path: Path) -> str:
    """Hash the installed package files carried by a built wheel."""
    files: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(wheel_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if not name.startswith(f"{PACKAGE_NAME}/") or name.endswith("/"):
                continue
            relative = PurePosixPath(name[len(PACKAGE_NAME) + 1:])
            if is_package_file(relative):
                files.append((relative.as_posix(), archive.read(info)))
    return _tree_digest(files)


def _project_metadata(checkout: Path) -> dict:
    """Read PEP 621 metadata from the checkout and fail closed when it is unusable."""
    pyproject = checkout / "pyproject.toml"
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read {pyproject}: {exc}") from exc
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{pyproject} must define a [project] table")
    for field in ("name", "version"):
        value = project.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{pyproject} [project] requires a non-empty {field}")
    return project


def _distribution_stem(project: dict) -> str:
    return f"{project['name'].replace('-', '_')}-{project['version']}"


def _metadata_text(project: dict, checkout: Path) -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {project['name']}",
        f"Version: {project['version']}",
    ]
    summary = project.get("description")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"Summary: {summary.strip()}")
    requires_python = project.get("requires-python")
    if isinstance(requires_python, str) and requires_python.strip():
        lines.append(f"Requires-Python: {requires_python.strip()}")
    license_value = project.get("license")
    if isinstance(license_value, str) and license_value.strip():
        lines.append(f"License: {license_value.strip()}")
    authors = project.get("authors")
    if isinstance(authors, list):
        for author in authors:
            if isinstance(author, dict) and isinstance(author.get("name"), str):
                lines.append(f"Author: {author['name']}")
    keywords = project.get("keywords")
    if isinstance(keywords, list) and keywords:
        lines.append("Keywords: " + ",".join(str(item) for item in keywords))
    classifiers = project.get("classifiers")
    if isinstance(classifiers, list):
        lines.extend(f"Classifier: {item}" for item in classifiers)
    urls = project.get("urls")
    if isinstance(urls, dict):
        for label, url in urls.items():
            lines.append(f"Project-URL: {label}, {url}")
    body = ""
    readme = project.get("readme")
    if isinstance(readme, str):
        readme_path = checkout / readme
        if readme_path.is_file():
            body = readme_path.read_text(encoding="utf-8")
            if not body.endswith("\n"):
                body += "\n"
            lines.append("Description-Content-Type: text/markdown")
    header = "\n".join(lines) + "\n"
    return f"{header}\n{body}" if body else header


def _entry_points_text(project: dict) -> str:
    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return ""
    for target in scripts.values():
        if not isinstance(target, str) or not target.strip():
            raise ValueError("pyproject.toml [project.scripts] values must be entry point strings")
    lines = ["[console_scripts]"]
    lines.extend(f"{name} = {scripts[name]}" for name in sorted(scripts))
    return "\n".join(lines) + "\n"


def _dist_info_entries(project: dict, checkout: Path) -> list[tuple[str, bytes, int]]:
    dist_info = f"{_distribution_stem(project)}.dist-info"
    entries: list[tuple[str, bytes]] = [
        (f"{dist_info}/METADATA", _metadata_text(project, checkout).encode("utf-8")),
        (f"{dist_info}/WHEEL", WHEEL_FILE_TEXT.encode("utf-8")),
        (f"{dist_info}/top_level.txt", f"{PACKAGE_NAME}\n".encode("utf-8")),
    ]
    entry_points = _entry_points_text(project)
    if entry_points:
        entries.append((f"{dist_info}/entry_points.txt", entry_points.encode("utf-8")))
    license_path = checkout / "LICENSE"
    if license_path.is_file():
        entries.append((f"{dist_info}/licenses/LICENSE", license_path.read_bytes()))
    return [(name, content, 0o100644) for name, content in entries]


def _payload_entries(
    project: dict, checkout: Path, *, editable: bool,
) -> list[tuple[str, bytes, int]]:
    entries: list[tuple[str, bytes, int]] = []
    if editable:
        source_path = (checkout / "src").resolve()
        entries.append((f"{PACKAGE_NAME}.pth", f"{source_path}\n".encode("utf-8"), 0o100644))
    else:
        src_root = checkout / "src" / PACKAGE_NAME
        for relative, path in iter_package_files(src_root):
            mode = 0o100755 if path.stat().st_mode & 0o111 else 0o100644
            entries.append((f"{PACKAGE_NAME}/{relative}", path.read_bytes(), mode))
    entries.extend(_dist_info_entries(project, checkout))
    return entries


def _temporary_sibling(destination: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
    )
    handle.close()
    return Path(handle.name)


def _record_row(name: str, content: bytes) -> tuple[str, str, str]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    return name, f"sha256={digest}", str(len(content))


def _record_text(rows: list[tuple[str, str, str]]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _write_wheel(
    destination: Path, entries: list[tuple[str, bytes, int]], record_name: str,
) -> None:
    record_rows = [_record_row(name, content) for name, content, _ in entries]
    record_rows.append((record_name, "", ""))
    record_rows.sort()
    record = _record_text(record_rows).encode("utf-8")
    payload = [*entries, (record_name, record, 0o100644)]
    temporary = _temporary_sibling(destination)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content, mode in payload:
                info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIMESTAMP)
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_wheel_from_checkout(
    checkout: Path, wheel_directory: Path, *, editable: bool = False,
) -> Path:
    """Build a wheel outside the checkout without touching build staging.

    The checkout's ``build/``, ``dist/`` and ``*.egg-info`` trees are never
    read or written. Raises ``ValueError`` when ``pyproject.toml`` has no
    usable ``[project]`` metadata or when no controlled package files exist.
    """
    project = _project_metadata(checkout)
    src_root = checkout / "src" / PACKAGE_NAME
    if not iter_package_files(src_root):
        raise ValueError(f"No controlled package files under {src_root}")
    wheel_directory.mkdir(parents=True, exist_ok=True)
    stem = _distribution_stem(project)
    tag = "0.editable-py3-none-any" if editable else "py3-none-any"
    destination = wheel_directory / f"{stem}-{tag}.whl"
    entries = _payload_entries(project, checkout, editable=editable)
    _write_wheel(destination, entries, f"{stem}.dist-info/RECORD")
    return destination


def _prepare_metadata(metadata_directory: Path, checkout: Path) -> str:
    project = _project_metadata(checkout)
    dist_info = f"{_distribution_stem(project)}.dist-info"
    for name, content, _ in _dist_info_entries(project, checkout):
        relative = name[len(dist_info) + 1:]
        path = metadata_directory / dist_info / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return dist_info


def get_requires_for_build_wheel(config_settings: dict | None = None) -> list[str]:
    """The wheel build has no third-party requirements."""
    return []


def get_requires_for_build_editable(config_settings: dict | None = None) -> list[str]:
    """The editable build has no third-party requirements."""
    return []


def get_requires_for_build_sdist(config_settings: dict | None = None) -> list[str]:
    """The source distribution build has no third-party requirements."""
    return []


def prepare_metadata_for_build_wheel(
    metadata_directory: str, config_settings: dict | None = None,
) -> str:
    """Write wheel metadata and return the created dist-info directory name."""
    return _prepare_metadata(Path(metadata_directory), Path.cwd())


def prepare_metadata_for_build_editable(
    metadata_directory: str, config_settings: dict | None = None,
) -> str:
    """Write editable-install metadata and return the dist-info directory name."""
    return _prepare_metadata(Path(metadata_directory), Path.cwd())


def build_wheel(
    wheel_directory: str,
    config_settings: dict | None = None,
    metadata_directory: str | None = None,
) -> str:
    """PEP 517 hook: build a wheel from the checkout in the current directory."""
    return build_wheel_from_checkout(Path.cwd(), Path(wheel_directory)).name


def build_editable(
    wheel_directory: str,
    config_settings: dict | None = None,
    metadata_directory: str | None = None,
) -> str:
    """PEP 660 hook: build an editable wheel exposing the checkout through a .pth."""
    return build_wheel_from_checkout(Path.cwd(), Path(wheel_directory), editable=True).name


def build_sdist(sdist_directory: str, config_settings: dict | None = None) -> str:
    """PEP 517 hook: build a source archive without build staging residue."""
    checkout = Path.cwd()
    project = _project_metadata(checkout)
    src_root = checkout / "src" / PACKAGE_NAME
    package_files = iter_package_files(src_root)
    if not package_files:
        raise ValueError(f"No controlled package files under {src_root}")
    stem = _distribution_stem(project)
    destination_directory = Path(sdist_directory)
    destination_directory.mkdir(parents=True, exist_ok=True)
    archive_entries: list[tuple[str, bytes, int]] = [
        (name, (checkout / name).read_bytes(), 0o644)
        for name in SOURCE_DOCUMENTS
        if (checkout / name).is_file()
    ]
    archive_entries.extend(
        (
            f"src/{PACKAGE_NAME}/{relative}",
            path.read_bytes(),
            0o755 if path.stat().st_mode & 0o111 else 0o644,
        )
        for relative, path in package_files
    )
    archive_entries.append(
        ("PKG-INFO", _metadata_text(project, checkout).encode("utf-8"), 0o644)
    )
    destination = destination_directory / f"{stem}.tar.gz"
    temporary = _temporary_sibling(destination)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for relative, content, mode in sorted(archive_entries):
                info = tarfile.TarInfo(f"{stem}/{relative}")
                info.size = len(content)
                info.mtime = 0
                info.mode = mode
                archive.addfile(info, io.BytesIO(content))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.name
