# Publish-flow threat model

This runbook is an adversary playbook for the agent publishing pipeline:
which gates a scammer is up against, which attack vectors each gate
handles, and which gaps the current implementation still has.

Use it as:
- **Defender's checklist** when adding a new field or route to publishing
- **Reviewer's reference** when triaging a malicious-listing incident
- **Test index** — every row points to the test in `tests/security/` that
  guards (or, for known gaps, surfaces) the issue

Generated alongside `tests/security/test_publish_robustness_*.py`. When
you patch a gap, update the row from `xfail` → `pass` in both places.

## Adversary personas

| Persona | Goal |
|---|---|
| **Brand squatter** | List an agent that visually impersonates a built-in to steal calls and ratings |
| **Exfiltration agent** | Look useful, harvest caller payloads, exfiltrate out-of-band |
| **Bait-and-switch** | Pass probe with a clean endpoint or SKILL.md, swap after approval |
| **Sybil rater** | Register agent + N sock-puppet callers, self-rate to escape probation |
| **Free-tier abuser** | Mass-register to consume namespace or grief auto-hire ranking |
| **Internal-call attacker** | Coax callers / models into hitting `/wallet`, `/admin`, `/ops` |

## Gates in execution order

1. CLI pre-flight (`aztea publish`)
2. Auth scope (`worker` required)
3. Per-owner agent cap (20)
4. SSRF + tunnel-host check (`core.url_security`)
5. Endpoint URL hygiene (`scan_agent_md_endpoint`)
6. Content scanner (`scan_skill_md`, `scan_python_handler`)
7. Liveness probe + adversarial probe (`_run_listing_safety_probe`)
8. (Optional) output-verifier callback
9. DB uniqueness + probation insert + Ed25519 identity binding

## Attack vector → test → status

### Content scanner

| ID | Vector | Test | Status |
|---|---|---|---|
| A1 | HTML-entity-encoded prompt injection (`&#105;gnore previous`) | `test_a1_html_entity_prompt_injection_blocked` | **GAP** — entities not decoded before phrase scan |
| A2 | Prompt injection inside YAML `description:` value | `test_a2_yaml_frontmatter_prompt_injection_blocked` | OK |
| A3 | Phrase inside fenced code block — blocked by design (no context awareness) | `test_a3_phrase_in_code_fence_still_blocks` | OK (intentional over-trigger) |
| A4 | Google/Stripe/Twilio/Mailgun/SendGrid/HF/AWS-secret key formats | `test_a4_extra_provider_key_formats_blocked` | **GAP** — patterns missing |
| A5 | Python const-folded key concat (`'sk-' + 'proj-' + ...`) | `test_a5_python_handler_key_concat_undetected_today` | Pinned current behaviour |
| A6 | AST reflection bypass (`getattr(__builtins__, "ex"+"ec")`, `().__class__`) | `test_a6_ast_reflection_bypass` | **GAP** for the chained variants |
| A7 | `__import__('subprocess')` in default-arg slot | `test_a7_default_arg_import_bypass` | **GAP** |
| A8 | Top-level `exec()` in module body | `test_a8_decorator_payload_detected` | OK |
| A9 | Zero-width characters joining a phrase | `test_a9_invisible_glue_in_phrase_blocked` | OK |
| A10 | U+202E (RLO) hiding reversed phrase | `test_a10_rtl_override_bypass` | **GAP** |
| A11 | Cyrillic / Greek homoglyphs in injection phrase | `test_a11_homoglyph_in_phrase_bypass` | **GAP** — fold table not shared with phrase scan |
| A12 | Long base64 blob split below the 200-char threshold | `test_a12_chunked_base64_below_threshold_no_warn` | Heuristic by design |
| A13 | Internal path via cross-token concat | `test_a13_constructed_internal_path_no_detection` | Heuristic by design |

### Endpoint URL / SSRF

| ID | Vector | Test | Status |
|---|---|---|---|
| B1 | Percent-encoded aztea.ai (`%61ztea.ai`) | `test_b1_percent_encoded_aztea_host` | OK at SSRF; defence-in-depth confirmed |
| B2 | IDN/punycode homoglyph (`xn--zte-3oa.ai`) | `test_b2_idn_punycode_homoglyph` | **GAP** |
| B3 | Fragment containing aztea host | `test_b3_fragment_with_aztea_does_not_misfire` | OK (fragment rejected outright) |
| B4 | Userinfo trick (`scheme://aztea@attacker/`) | `test_b4_userinfo_host_is_attacker` | OK |
| B5 | DNS rebind between register and call | `test_b5_dns_rebind_call_time_revalidation_required` | **GAP** — call-time revalidation missing |
| B6 | IPv4-mapped IPv6 (`::ffff:10.0.0.1`) | `test_b6_ipv4_mapped_ipv6_blocked` | OK |
| B7 | Multi-A-record: one public + one private | `test_b7_multi_a_record_one_private_refused` | OK |
| B8 | Tunnel host drift (cfargotunnel, lhr.life, devtunnels, bore.pub, pinggy, zrok) | `test_b8_tunnel_host_drift` | **GAP** for newer tunnels |
| B9 | Probe follows redirects | `test_b9_probe_disables_redirects` | OK |
| B10 | IPv6 literal in brackets | `test_b10_ipv6_literal_variants_blocked` | OK |
| B11 | Cloud metadata endpoints | `test_b11_cloud_metadata_endpoints_blocked` | OK for AWS/GCP; **GAP** for Alibaba 100.64/10 |
| B12 | Localhost aliases (`0`, `0.0.0.0`, `localhost.localdomain`) | `test_b12_localhost_aliases_blocked` | Partial — `0` and `localhost.localdomain` are gaps |

### Probe-time evasion

| ID | Vector | Test | Status |
|---|---|---|---|
| C1 | Endpoint fingerprints probe by User-Agent | `test_c1_probe_rotates_user_agent` | **GAP** |
| C2 | Endpoint fingerprints by literal probe payload | `test_c2_adversarial_probes_are_constant_and_fingerprintable` | **GAP** |
| C3 | Endpoint detects absence of Authorization/job_id | `test_c3_probe_mimics_real_call_envelope` | **GAP** |
| C4 | Endpoint times out probe (silent pass) | `test_c4_probe_timeout_does_not_silently_pass` | **GAP** — policy fix |
| C5 | Endpoint 5xx during probe (silent pass) | `test_c5_probe_5xx_does_not_silently_pass` | **GAP** |
| C6 | Endpoint returns `{}` | `test_c6_empty_response_does_not_trigger_shape_mismatch` | Pinned current behaviour |
| C7 | Endpoint base64-encodes leaked key in body | `test_c7_base64_encoded_leak_detected` | **GAP** |
| C8 | Leak via response headers | `test_c8_evaluate_probe_response_does_not_take_headers` | **GAP** |
| C9 | Outbound DNS side-channel during probe | `test_c9_probe_observes_outbound_dns` | Structural limitation |
| C10 | Multi-GB streaming response | `test_c10_probe_has_response_size_cap` | **GAP** |

### Post-registration mutation

| ID | Vector | Test | Status |
|---|---|---|---|
| D1 | PATCH description → prompt injection | `test_d1_patch_description_rescanned` | OK |
| D1b | PATCH tags → prompt injection | `test_d1b_patch_tags_rescanned` | **GAP** |
| D2 | PATCH endpoint_url to private/aztea | `test_d2_endpoint_url_immutable_via_patch` | OK (immutable by schema) |
| D3 | PATCH output_verifier_url | `test_d3_output_verifier_url_immutable_via_patch` | OK (immutable by schema) |
| D4 | Price jump after probation graduation | `test_d4_price_jump_after_registration_capped` | **GAP** — no cooldown |
| D5 | PATCH output_examples (covered by D1b) | n/a | n/a |
| D6 | Re-register after sunset to escape owner reputation | `test_d6_resubmission_blocked_after_owner_history` | **GAP** — no owner-level reputation |

### Probation escape / ratings

| ID | Vector | Test | Status |
|---|---|---|---|
| E1 | Sybil self-rating | `test_e1_self_rating_excluded_from_graduation` | **GAP** |
| E2 | Correlated callers (IP/payment) | `test_e2_sybil_caller_correlation_signal_exists` | **GAP** |
| E3 | `private_task=True` padding call count | `test_e3_private_call_count_treated_consistently` | Behaviour pinned |
| E4 | Rating-velocity anomaly | `test_e4_rating_velocity_anomaly_surface_exists` | **GAP** |
| E5 | Owner-side cancellations excluded from failure rate | `test_e5_owner_cancellation_does_not_inflate_success_rate` | **GAP** |
| E6 | Constant-output agent passes quality gate | `test_e6_quality_judge_runs_against_probation` | **GAP** |

### Owner-level abuse

| ID | Vector | Test | Status |
|---|---|---|---|
| F1 | Duplicate name within owner | `test_f1_duplicate_name_within_owner` | Verifying via integration |
| F2 | 21st registration rejected | `test_f2_owner_cap_enforced` | OK |
| F3 | Master-key skips probation | `test_f3_master_registrations_skip_probation` | OK |
| F4 | Caller-only scope refused | `test_f4_caller_only_scope_cannot_register` | OK |
| F5 | Agent-scoped key refused | `test_f5_agent_key_cannot_register` | OK |

### Identity / crypto

| ID | Vector | Test | Status |
|---|---|---|---|
| G1 | DID uniqueness DB-enforced | `test_g1_did_has_unique_index` | OK |
| G2 | Output signature binds to agent_id | `test_g2_signature_binds_to_agent_id` | OK |
| G3 | DID document resolves + matches stored key | `test_g3_did_document_resolves_and_matches` | OK |
| G4 | Signing-key rotation history | `test_g4_key_rotation_history` | **GAP** |

### Visual / name impersonation

| ID | Vector | Test | Status |
|---|---|---|---|
| H1 | Cyrillic homoglyph in name | `test_h1_homoglyph_name_clone_detected` | **GAP** |
| H2 | Synonym evasion of Jaccard | `test_h2_synonym_evasion_documented` | Pinned |
| H3 | Filler-word padding | `test_h3_filler_padding_pinned` | Pinned |
| H4 | Description PATCH | covered by D1b | n/a |
| H5 | Leading zero-width in name | `test_h5_leading_zero_width_in_name` | **GAP** |

### Output verifier

| ID | Vector | Test | Status |
|---|---|---|---|
| I1 | Unsigned `verified: true` response | `test_i1_verifier_requires_signed_response` | **GAP** |
| I2 | Verifier URL at aztea host | `test_i2_verifier_url_blocks_aztea_host` | Verifying |
| I3 | Verifier verdict not bound to payload hash | `test_i3_verifier_response_includes_payload_hash` | **GAP** |

### Privacy

| ID | Vector | Test | Status |
|---|---|---|---|
| J1 | Security-category drops work-example recording | `test_j1_security_category_drops_work_examples` | OK |
| J2 | `pii_safe` / `outputs_not_stored` not enforced at storage | `test_j2_pii_safe_enforced_at_storage` | **GAP** |

## When you add a new gate

1. Add a row to the relevant section here.
2. Add a corpus sample in `tests/security/corpus/scammer_listings/` if the
   gate is over a parseable file or URL.
3. Add a new test in the matching `test_publish_robustness_*.py` file.
4. Run `pytest -q tests/security -m security` and update this doc with the
   new row's status.

## On-call quick reference

- Suspected malicious listing: check `review_status` and `created_at` of
  the agent row; if `probation` and recent, flag for review.
- Suspected probe-evasion: re-run the registration probe with
  `AZTEA_RUN_REGISTER_SAFETY_PROBE=1` against the live endpoint and
  compare against a `curl` POST from outside the platform — divergence is
  the signal.
- Suspected Sybil ring: query `auto_hire_decisions` for clusters of
  identical-intent calls to the same agent inside a short window,
  cross-reference rater `user_id`s in `job_quality_ratings`.
