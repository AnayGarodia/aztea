"""quant-bench package — evaluation corpus for `quant_patch_validator`.

# OWNS: providing a deterministic, file-based corpus of (pre, gold,
#        candidates, labels) tuples and a thin scoring harness that
#        computes precision / recall / false-alarm-rate against it.
# NOT OWNS: the validator itself (lives under `agents/quant_patch_validator/`).
# INVARIANTS:
#   - Every entry directory contains pre.py, gold.py, candidates/, labels.json.
#   - labels.json declares one of {correct, regression, broken} for every
#     candidate file present. Mislabelled entries silently corrupt the
#     metrics — the loader validates the mapping is total.
# DECISIONS:
#   - Corpus is plain Python files, not pickled — diffability matters for
#     a quant-firm audit.
# KNOWN DEBT:
#   - score.py runs candidates sequentially. With ~30 entries × ~3
#     candidates we eat ~90 fuzz cycles; parallelisation will help once
#     the bench grows.
"""

from benchmarks.quant_bench.loader import Entry, iter_entries, load_entry

__all__ = ["Entry", "iter_entries", "load_entry"]
