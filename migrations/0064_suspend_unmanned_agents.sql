-- 0064_suspend_unmanned_agents.sql
-- 2026-05-20: Suspend two community-registered agents that have no live
-- worker pool. External grading audit found both stuck in 'pending'
-- indefinitely (semantic_codebase_search 14% historical success,
-- multi_file_python_executor same failure mode). Selling them as live
-- while charging escrow is the worst-of-both for caller trust.
--
-- Suspension hides them from list_agents / search_specialists (filtered
-- by status='active' in core/registry/agents_ops.py) and from auto-hire
-- ranking. Historical jobs and receipts remain resolvable.
--
-- To restore run: UPDATE agents SET status='active', suspension_reason=NULL
-- WHERE name IN (...) — fully reversible, no data dropped.

UPDATE agents
SET status = 'suspended',
    suspension_reason = 'worker_pool_starved_2026_05_20'
WHERE name IN ('semantic_codebase_search', 'multi_file_python_executor')
  AND status = 'active';
