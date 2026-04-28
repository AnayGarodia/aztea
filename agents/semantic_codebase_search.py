"""
semantic_codebase_search.py — Embed a local tarball/zip codebase and search by natural-language query

Input:
  {
    "query": "PDF text extraction",           # natural-language search query (required)
    "artifact": {                             # tarball/zip artifact (mutually exclusive with git_url)
        "url_or_base64": "data:application/zip;base64,...",
        "name": "repo.zip"
    },
    "git_url": "https://github.com/org/repo",  # public git URL (mutually exclusive with artifact)
    "top_k": 5,                               # number of results (1–20, default 5)
    "extensions": [".py", ".ts", ".md"],      # optional file extension filter
    "max_file_bytes": 102400                  # max bytes per file to embed (default 100 KB)
  }

Output:
  {
    "query": str,
    "total_files_indexed": int,
    "results": [
      {"path": str, "score": float, "snippet": str, "size_bytes": int}
    ],
    "source": "artifact|git"
  }
"""
from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from typing import Any

from core import url_security
from core.embeddings import cosine, embed_texts_batch
from core.feature_flags import DISABLE_EMBEDDINGS

_DEFAULT_TOP_K = 5
_MAX_TOP_K = 20
_DEFAULT_MAX_FILE_BYTES = 100_000
_SNIPPET_CHARS = 400
_DEFAULT_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java", ".cpp", ".c", ".h", ".md", ".txt", ".yaml", ".yml", ".json", ".toml"}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _file_text(data: bytes, max_bytes: int) -> str:
    if len(data) > max_bytes:
        data = data[:max_bytes]
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_files_from_zip(data: bytes, extensions: set[str], max_file_bytes: int) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                _, ext = os.path.splitext(info.filename.lower())
                if extensions and ext not in extensions:
                    continue
                try:
                    content = zf.read(info.filename)
                    text = _file_text(content, max_file_bytes)
                    if text.strip():
                        # Strip leading archive-root prefix so paths are relative
                        path = re.sub(r"^[^/]+/", "", info.filename)
                        files[path or info.filename] = text
                except Exception:
                    continue
    except zipfile.BadZipFile:
        pass
    return files


def _extract_files_from_tarball(data: bytes, extensions: set[str], max_file_bytes: int) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                _, ext = os.path.splitext(member.name.lower())
                if extensions and ext not in extensions:
                    continue
                try:
                    fobj = tf.extractfile(member)
                    if fobj is None:
                        continue
                    content = fobj.read()
                    text = _file_text(content, max_file_bytes)
                    if text.strip():
                        path = re.sub(r"^[^/]+/", "", member.name)
                        files[path or member.name] = text
                except Exception:
                    continue
    except tarfile.TarError:
        pass
    return files


def _extract_artifact(artifact: dict[str, Any], extensions: set[str], max_file_bytes: int) -> dict[str, str] | None:
    raw = str(artifact.get("url_or_base64") or "").strip()
    name = str(artifact.get("name") or "").lower()

    # Decode data URI or raw base64
    if raw.startswith("data:"):
        m = re.match(r"^data:[^;,]+;base64,([A-Za-z0-9+/=]+)$", raw, re.IGNORECASE)
        if not m:
            return None
        data = base64.b64decode(m.group(1))
    else:
        try:
            data = base64.b64decode(raw)
        except Exception:
            return None

    # Attempt zip first, then tarball
    if name.endswith(".zip") or raw.startswith("data:application/zip") or raw.startswith("data:application/x-zip"):
        files = _extract_files_from_zip(data, extensions, max_file_bytes)
        if files:
            return files
    files = _extract_files_from_tarball(data, extensions, max_file_bytes)
    if files:
        return files
    # Last attempt: try zip anyway
    return _extract_files_from_zip(data, extensions, max_file_bytes) or None


def _clone_git_repo(git_url: str, extensions: set[str], max_file_bytes: int) -> dict[str, str] | dict:
    try:
        git_url = url_security.validate_outbound_url(git_url, "git_url")
    except Exception as exc:
        return _err("semantic_codebase_search.url_blocked", str(exc))

    if not re.match(r"^https?://", git_url):
        return _err("semantic_codebase_search.invalid_git_url", "git_url must be an https:// URL.")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch", git_url, tmpdir],
                capture_output=True,
                timeout=60,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            return _err("semantic_codebase_search.clone_failed", f"git clone failed: {exc.stderr[:200] if exc.stderr else ''}")
        except FileNotFoundError:
            return _err("semantic_codebase_search.tool_unavailable", "git is not installed on this executor.")

        files: dict[str, str] = {}
        for root, _dirs, filenames in os.walk(tmpdir):
            # Skip .git directory
            _dirs[:] = [d for d in _dirs if d != ".git"]
            for filename in filenames:
                _, ext = os.path.splitext(filename.lower())
                if extensions and ext not in extensions:
                    continue
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, tmpdir)
                try:
                    with open(full_path, "rb") as f:
                        content = f.read()
                    text = _file_text(content, max_file_bytes)
                    if text.strip():
                        files[rel_path] = text
                except Exception:
                    continue
        return files


def run(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if not query:
        return _err("semantic_codebase_search.missing_query", "query is required.")
    if len(query) > 500:
        return _err("semantic_codebase_search.query_too_long", "query must be <= 500 characters.")

    top_k = max(1, min(int(payload.get("top_k") or _DEFAULT_TOP_K), _MAX_TOP_K))
    max_file_bytes = max(1024, min(int(payload.get("max_file_bytes") or _DEFAULT_MAX_FILE_BYTES), 500_000))

    ext_raw = payload.get("extensions")
    if isinstance(ext_raw, list) and ext_raw:
        extensions: set[str] = {str(e).lower() if str(e).startswith(".") else f".{e.lower()}" for e in ext_raw}
    else:
        extensions = _DEFAULT_EXTENSIONS

    artifact = payload.get("artifact")
    git_url = str(payload.get("git_url") or "").strip()

    if not artifact and not git_url:
        return _err("semantic_codebase_search.missing_source", "Provide artifact (zip/tarball) or git_url.")
    if artifact and git_url:
        return _err(
            "semantic_codebase_search.ambiguous_source",
            "Provide either artifact or git_url, not both.",
        )

    source = "artifact" if artifact else "git"
    if artifact:
        files = _extract_artifact(artifact, extensions, max_file_bytes)
        if files is None:
            return _err("semantic_codebase_search.extraction_failed", "Could not extract files from the provided artifact. Ensure it is a valid zip or tarball.")
    else:
        files = _clone_git_repo(git_url, extensions, max_file_bytes)
        if isinstance(files, dict) and "error" in files:
            return files

    if not files:
        return _err("semantic_codebase_search.no_files", "No text files found in the provided source.")

    # Embed the query
    if DISABLE_EMBEDDINGS:
        # Lexical fallback when embeddings are disabled
        q_terms = set(query.lower().split())
        scored: list[tuple[float, str, str]] = []
        for path, text in files.items():
            hits = sum(1 for term in q_terms if term in text.lower() or term in path.lower())
            if hits > 0:
                scored.append((float(hits) / len(q_terms), path, text))
    else:
        paths = list(files.keys())
        doc_texts = [f"{path}\n{files[path][:1000]}" for path in paths]
        all_vecs = embed_texts_batch([query] + doc_texts)
        query_vec = all_vecs[0]
        scored = []
        for i, path in enumerate(paths):
            score = cosine(query_vec, all_vecs[i + 1])
            if score > 0.1:
                scored.append((score, path, files[path]))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, path, text in scored[:top_k]:
        snippet = text[:_SNIPPET_CHARS].replace("\n", " ").strip()
        results.append({
            "path": path,
            "score": round(float(score), 4),
            "snippet": snippet,
            "size_bytes": len(text.encode("utf-8", errors="replace")),
        })

    return {
        "query": query,
        "total_files_indexed": len(files),
        "results": results,
        "source": source,
    }
