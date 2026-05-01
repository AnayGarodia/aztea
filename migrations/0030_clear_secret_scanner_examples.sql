-- Wipe historical secret_scanner work-examples (input field may contain raw secrets).
-- Future writes blocked in core via _record_public_work_example sensitivity gate.
UPDATE agents SET output_examples = NULL WHERE agent_id = '1021c65c-d2bf-54ff-823a-897f9deb1029';
