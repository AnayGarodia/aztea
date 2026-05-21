"""Coverage tracking — measure which branches of the candidate were hit.

# OWNS: wrapping the fuzz loop with coverage.py instrumentation that
#        tracks branches in the candidate source ONLY (not the reference
#        and not standard-library imports). Reports a coverage_pct
#        that surfaces "the AI patch left these branches untested" in
#        the agent's output.
# NOT OWNS: fuzz driving (fuzz.py), oracle math (harness.py).
# INVARIANTS:
#   - We never raise out of the coverage layer. coverage.py initialisation
#     failure (or any subsequent measurement issue) is logged and the
#     fuzz proceeds without coverage data — coverage is a nice-to-have,
#     not a hard requirement.
#   - We only instrument the SYNTHETIC candidate module, never the
#     surrounding Python (numpy, pandas, fuzz harness). Otherwise the
#     coverage data would be dominated by stdlib branches that aren't
#     meaningful to the caller.
# DECISIONS:
#   - coverage.py's `branch=True` mode measures both line and arc
#     coverage. Arc coverage matters more for our purposes: an `if`
#     that's never taken because the candidate has a logic gap is
#     exactly the kind of "AI patch missed an edge case" signal we
#     want to surface.
#   - We use a temporary file for the candidate source (rather than
#     `<qpv_cand>` in-memory) because coverage.py's data file model
#     keys on file paths. The temp file is deleted in the finally
#     block — leak-free.
# KNOWN DEBT:
#   - Doesn't yet identify WHICH branches were missed. The report
#     surfaces `coverage_pct` but not "line 42 of the candidate was
#     never executed." That's a v0.2 follow-up.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass

_LOG = logging.getLogger("aztea.agents.quant_patch_validator.coverage")


@dataclass
class CoverageResult:
    """Per-candidate coverage data attached to the agent's output."""

    available: bool
    coverage_pct: float | None  # 0..100, or None when unavailable
    statements: int
    executed: int
    missing_lines: list[int]  # candidate-source line numbers not executed


_DEFAULT = CoverageResult(
    available=False, coverage_pct=None, statements=0, executed=0, missing_lines=[]
)


@contextmanager
def candidate_coverage(candidate_source: str):
    """Run a block with coverage.py wrapping the candidate.

    Usage:
        with candidate_coverage(cand_src) as ctx:
            # build harness using ctx.candidate_path as the candidate file
            # (the source loaded from that file is the same string;
            #  coverage.py keys on file path)
            ...
        result = ctx.result()
    """
    handle = _CoverageHandle(candidate_source)
    try:
        handle.start()
        yield handle
    finally:
        handle.stop()


class _CoverageHandle:
    """Helper object yielded by `candidate_coverage`."""

    def __init__(self, candidate_source: str) -> None:
        self._source = candidate_source
        self._cov = None
        self._tmp_path: str | None = None

    @property
    def candidate_path(self) -> str | None:
        return self._tmp_path

    def start(self) -> None:
        try:
            import coverage  # type: ignore[import]
        except ImportError:
            return
        try:
            fd, path = tempfile.mkstemp(prefix="qpv_cand_", suffix=".py")
            os.close(fd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._source)
            self._tmp_path = path
            self._cov = coverage.Coverage(
                # include= (not source=) matches the candidate's resolved
                # file path even when loaded via importlib.util — source=
                # uses a package-style filter that excludes tempfile paths.
                include=[path],
                branch=True,
                data_file=None,
                messages=False,
            )
            self._cov.start()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("coverage.py init failed; continuing without coverage: %s", exc)
            self._cleanup_tmp()
            self._cov = None

    def stop(self) -> None:
        if self._cov is not None:
            try:
                self._cov.stop()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("coverage.py stop failed: %s", exc)

    def result(self) -> CoverageResult:
        if self._cov is None or self._tmp_path is None:
            self._cleanup_tmp()
            return _DEFAULT
        try:
            # coverage.py's analysis() returns (filename, statements,
            # excluded, missing_lines, missing_branches_textual).
            analysis = self._cov.analysis2(self._tmp_path)
            statements = analysis[1]
            missing = analysis[3]
            n_stmts = len(statements)
            n_missing = len(missing)
            executed = n_stmts - n_missing
            pct = (100.0 * executed / n_stmts) if n_stmts else None
            return CoverageResult(
                available=True,
                coverage_pct=round(pct, 2) if pct is not None else None,
                statements=n_stmts,
                executed=executed,
                missing_lines=list(missing),
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("coverage.py analysis failed: %s", exc)
            return _DEFAULT
        finally:
            self._cleanup_tmp()

    def _cleanup_tmp(self) -> None:
        if self._tmp_path:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass
            self._tmp_path = None
