-- Track when a dispute first entered the 'tied' state so the background loop
-- can auto-rule in favour of the caller after 48 hours.
ALTER TABLE disputes ADD COLUMN tied_since TEXT;
