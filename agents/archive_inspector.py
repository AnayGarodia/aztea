"""
Archive inspector agent — inspects ZIP and tar archives for structure,
metadata, and security risks without extracting files to disk.

Inputs:
  content_base64 (str): base64-encoded archive bytes
  filename (str, optional): hint for format detection
  max_entries (int, optional): cap on listed entries, default 500

Outputs: dict with format, entry list, security flags, and largest entries.

External dependencies: stdlib only (zipfile, tarfile, gzip, bz2, lzma).
Runtime requirements: none beyond Python 3.9+.
"""

# OWNS: inspecting ZIP and tar archives for structure, metadata, and security risks without extraction
# NOT OWNS: extracting archive contents to disk, creating archives
# INVARIANTS: never writes extracted files to disk; reads only; max archive size 50MB
# DECISIONS: in-memory inspection only — avoids disk I/O and path traversal during analysis

import base64
import io
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 500
BOMB_RATIO_THRESHOLD = 100
BOMB_SIZE_THRESHOLD = 100_000_000
MAX_DEPTH = 10
TOP_LARGEST_COUNT = 5
SUSPICIOUS_EXTENSIONS = frozenset(
    {".exe", ".bat", ".sh", ".ps1", ".cmd", ".vbs", ".scr", ".dll", ".dylib", ".so"}
)

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
_BZ2_MAGIC = b"BZ"
_XZ_MAGIC = b"\xfd7zXZ\x00"
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC_VALUES = (b"ustar\x00", b"ustar ")

_EXTENSION_FORMAT_MAP = {
    ".zip": "zip",
    ".tar": "tar",
    ".gz": "tar.gz",
    ".tgz": "tar.gz",
    ".bz2": "tar.bz2",
    ".tbz2": "tar.bz2",
    ".xz": "tar.xz",
    ".txz": "tar.xz",
}

_FORMAT_TAR_MODE = {
    "tar": "r:",
    "tar.gz": "r:gz",
    "tar.bz2": "r:bz2",
    "tar.xz": "r:xz",
}


def _error(code: str, message: str) -> dict:
    """Return a structured error envelope."""
    return {"error": {"code": code, "message": message}}


def _detect_format(raw: bytes, filename: str) -> str | None:
    """Detect archive format from magic bytes, falling back to filename extension."""
    if any(raw[:4].startswith(m) for m in _ZIP_MAGIC):
        return "zip"
    if raw[:2] == _GZIP_MAGIC:
        return "tar.gz"
    if raw[:2] == _BZ2_MAGIC:
        return "tar.bz2"
    if raw[:6] == _XZ_MAGIC:
        return "tar.xz"
    slice_257 = raw[_TAR_MAGIC_OFFSET : _TAR_MAGIC_OFFSET + 6]
    if slice_257 in _TAR_MAGIC_VALUES:
        return "tar"
    # Fall back to filename extension
    if filename:
        suffix = "".join(PurePosixPath(filename).suffixes[-2:])
        for ext, fmt in _EXTENSION_FORMAT_MAP.items():
            if suffix.endswith(ext):
                return fmt
    return None


def _zip_modified(date_time: tuple) -> str | None:
    """Convert ZIP date_time tuple to ISO-8601 string."""
    try:
        dt = datetime(*date_time[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def _tar_modified(mtime: float) -> str | None:
    """Convert tar Unix mtime to ISO-8601 string."""
    try:
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def _octal_mode(mode: int) -> str:
    """Format a numeric Unix mode as an octal string."""
    return oct(mode)


def _path_depth(path: str) -> int:
    """Count slash-separated components in a path."""
    return len([p for p in path.split("/") if p])


def _security_flags(entries: list[dict], total_uncomp: int, total_comp: int) -> dict:
    """Compute all security flags from the full entry list."""
    path_traversal: list[str] = []
    absolute_paths: list[str] = []
    suspicious_exts: list[str] = []
    symlinks: list[str] = []
    deep_entries: list[str] = []

    for entry in entries:
        path = entry["path"]
        parts = [p for p in path.split("/") if p]
        if ".." in parts:
            path_traversal.append(path)
        if path.startswith("/"):
            absolute_paths.append(path)
        ext = PurePosixPath(path).suffix.lower()
        if ext in SUSPICIOUS_EXTENSIONS:
            suspicious_exts.append(path)
        if entry.get("symlink_target") is not None:
            symlinks.append(path)
        if _path_depth(path) > MAX_DEPTH:
            deep_entries.append(path)

    bomb_risk = (
        total_comp > 0
        and (total_uncomp / total_comp) > BOMB_RATIO_THRESHOLD
        and total_uncomp > BOMB_SIZE_THRESHOLD
    )

    return {
        "zip_bomb_risk": bomb_risk,
        "path_traversal_entries": path_traversal,
        "absolute_path_entries": absolute_paths,
        "suspicious_extensions": suspicious_exts,
        "symlink_entries": symlinks,
        "deeply_nested_entries": deep_entries,
    }


def _inspect_zip(raw: bytes, max_entries: int) -> dict:
    """Inspect a ZIP archive in-memory and return the structured result."""
    buf = io.BytesIO(raw)
    all_entries: list[dict] = []
    total_uncomp = 0
    total_comp = 0

    with zipfile.ZipFile(buf) as zf:
        for info in zf.infolist():
            total_uncomp += info.file_size
            total_comp += info.compress_size
            unix_mode = info.external_attr >> 16
            entry = {
                "path": info.filename,
                "size_bytes": info.file_size,
                "compressed_bytes": info.compress_size,
                "is_dir": info.is_dir(),
                "mode": _octal_mode(unix_mode) if unix_mode else None,
                "modified": _zip_modified(info.date_time),
                "symlink_target": None,
            }
            all_entries.append(entry)

    truncated = len(all_entries) > max_entries
    ratio = (total_uncomp / total_comp) if total_comp > 0 else None
    largest = _top_largest(all_entries)
    security = _security_flags(all_entries, total_uncomp, total_comp)

    return {
        "format": "zip",
        "total_entries": len(all_entries),
        "total_uncompressed_bytes": total_uncomp,
        "total_compressed_bytes": total_comp,
        "compression_ratio": ratio,
        "entries": all_entries[:max_entries],
        "truncated": truncated,
        "security": security,
        "largest_entries": largest,
    }


def _inspect_tar(raw: bytes, fmt: str, max_entries: int) -> dict:
    """Inspect a tar (plain, gzip, bz2, or xz) archive in-memory."""
    mode = _FORMAT_TAR_MODE[fmt]
    buf = io.BytesIO(raw)
    all_entries: list[dict] = []
    total_uncomp = 0

    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for member in tf.getmembers():
            total_uncomp += member.size
            symlink_target = member.linkname if member.issym() else None
            entry = {
                "path": member.name,
                "size_bytes": member.size,
                "compressed_bytes": 0,
                "is_dir": member.isdir(),
                "mode": _octal_mode(member.mode) if member.mode else None,
                "modified": _tar_modified(member.mtime),
                "symlink_target": symlink_target,
            }
            all_entries.append(entry)

    truncated = len(all_entries) > max_entries
    # tar has no per-file compression metadata; use raw bytes as proxy
    total_comp = len(raw)
    ratio = (total_uncomp / total_comp) if (total_comp > 0 and fmt != "tar") else None
    largest = _top_largest(all_entries)
    security = _security_flags(all_entries, total_uncomp, total_comp)

    return {
        "format": fmt,
        "total_entries": len(all_entries),
        "total_uncompressed_bytes": total_uncomp,
        "total_compressed_bytes": 0,
        "compression_ratio": ratio,
        "entries": all_entries[:max_entries],
        "truncated": truncated,
        "security": security,
        "largest_entries": largest,
    }


def _top_largest(entries: list[dict]) -> list[str]:
    """Return the top N entry paths by uncompressed size."""
    sorted_entries = sorted(entries, key=lambda e: e["size_bytes"], reverse=True)
    return [e["path"] for e in sorted_entries[:TOP_LARGEST_COUNT]]


def _decode_content(payload: dict) -> bytes | dict:
    """Decode base64 content from payload; return error dict on failure."""
    raw_b64 = payload.get("content_base64")
    if not raw_b64:
        return _error("archive_inspector.missing_content", "content_base64 is required")
    try:
        return base64.b64decode(raw_b64)
    except Exception:
        return _error("archive_inspector.decode_failed", "content_base64 could not be decoded")


def run(payload: dict) -> dict:
    """
    Inspect a ZIP or tar archive from base64-encoded bytes.

    Performs in-memory analysis only — no files are written to disk.
    Returns entry metadata, compression stats, and security risk flags.
    """
    raw = _decode_content(payload)
    if isinstance(raw, dict):
        return raw

    if len(raw) > MAX_ARCHIVE_BYTES:
        return _error(
            "archive_inspector.archive_too_large",
            f"Decoded archive exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)}MB limit",
        )

    filename = payload.get("filename", "")
    max_entries = int(payload.get("max_entries") or DEFAULT_MAX_ENTRIES)
    fmt = _detect_format(raw, filename)

    if fmt is None:
        return _error(
            "archive_inspector.unsupported_format",
            "Could not detect archive format from magic bytes or filename extension",
        )

    try:
        if fmt == "zip":
            return _inspect_zip(raw, max_entries)
        return _inspect_tar(raw, fmt, max_entries)
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        return _error(
            "archive_inspector.unsupported_format",
            f"Archive could not be opened: {exc}",
        )
