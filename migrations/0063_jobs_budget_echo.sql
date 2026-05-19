-- F8 (red-team 2026-05-19): persist the caller-submitted budget_cents /
-- max_price_cents value so JobResponse can echo it back to the caller.
-- Pre-fix the validators consumed the field at request time but never
-- stored it, so a caller submitting "budget_cents: 5" against a 1cent
-- agent could not verify the cap landed.
ALTER TABLE jobs ADD COLUMN budget_cents INTEGER
