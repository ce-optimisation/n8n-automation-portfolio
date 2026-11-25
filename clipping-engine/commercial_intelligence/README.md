# Local campaign brief assessment

Run from the Clipping workspace with Python 3.10 or later.

    python -m commercial_intelligence.assess path/to/brief.json
    python -m unittest commercial_intelligence.test_assess -v

The tool consumes manually structured, evidence-labelled JSON. It performs local arithmetic and checks, with no network calls, platform credentials, or publishing actions. Its eligibility result reflects submitted evidence labels. It does not independently verify a licence, date, evidence source or account. Review campaign enhancements outside the standard check list by adding named checks of your own.

## Input schema

| Key | Format |
| --- | --- |
| campaign | Non-empty campaign name or ID. |
| checks | Object keyed by check. Values contain a status of met, unmet or unknown, and an evidence string for any definite status. Prefer a dated source or stored reference. Missing standard checks become unknown. |
| economics | Model fixed_cpm or shared_pool, three-letter uppercase currency, incremental_cost_minor and an evidence reference. Fixed CPM also requires cpm_minor and fee_basis_points. |
| approved_view_scenarios | Non-empty array of non-negative integers for hypothetical approved views. |

Standard checks are campaign_open, account_eligible, region_eligible, footage_authorized, destination_authorized, editing_rules_satisfied and submission_deadline_met.

Fixed CPM accepts optional minimum_payout_minor, maximum_payout_minor and remaining_budget_minor. A missing or null term is unknown and makes the estimate bounded only by supplied terms. Use zero for a verified zero amount. The estimate floors gross to a minor unit, applies supplied budget and payout caps, then applies the minimum payout threshold. Fees round up to a minor unit. Contribution subtracts fee and incremental cost. Include transfer charges in costs only if appropriate. Confirm campaign-specific thresholds, fee basis and budget rules before relying on a scenario. The tool does not project a shared-pool amount based on approved views alone.

The output always distinguishes estimated contribution from actual earnings, which remain null, and publication authorization, which remains false. Invalid input produces a JSON error on stderr with exit status 2.

## Candidate selection and rights preflight

    python -m commercial_intelligence.select_candidates path/to/brief.json path/to/clips.json
    python -m unittest discover -s commercial_intelligence -v

The candidate command adds a local review shortlist to the assessment output. It works against supplied metadata, without loading the production database, reading the media, or contacting a platform. Supply `--as-of 2026-11-25T12:00:00Z` to reproduce a decision at a known UTC time. Otherwise it uses current UTC.

Add `selection_rules` to the same brief:

| Field | Format |
| --- | --- |
| evidence | Brief URL or a durable evidence reference. |
| minimum_duration_ms, maximum_duration_ms | Positive integer thresholds, inclusive. |
| submission_deadline | Exclusive UTC timestamp. Normalize any published deadline to UTC before entry. |
| destinations | Object mapping exact platform/account keys to evidence references. |
| source_windows | Array of objects containing `source_id`, `start_ms`, `end_ms`, and `evidence`. Use a null end only if the brief explicitly permits footage through the end. |

The second file is an array with one row per candidate clip and intended destination for this run. Each row has a positive unique `clip_id`, immutable `source_id`, exact `destination`, lifecycle `status`, `safety_status`, and `start_ms`/`end_ms` relative to the source. An optional integer `quality_score` from 0 to 100 orders the review list. An optional `rights` object records a `status` of met, unmet or unknown. Definite rights evidence names `campaign`, `source_id`, `destination`, extracts the covered interval in `start_ms`/`end_ms`, and cites a durable `evidence` reference. Include `expires_at` where known. Use a null end for an explicitly open-ended permission and omit the whole rights object when scope cannot be verified. An acquisition-only licence is not redistribution evidence.

Only ready or approved clips with clear safety status and in-scope rights evidence can be shortlisted. Each excerpt must fit wholly inside a single allowed source window. Unmet checks exclude a candidate. Unknown evidence holds a candidate for review. The result includes the actual rules, destination and rights evidence references needed to trace a decision. It does not change clip approval, alter renders, authorise publication or submit anything. Creative requirements not captured by the standard fields still belong in the brief's named evidence checks.
## Render, handoff and settlement

    python -m commercial_intelligence.media_check clip.mp4 --width 1080 --height 1920 --min-seconds 15 --max-seconds 60
    python -m commercial_intelligence.submission brief.json clips.json clip.mp4 --clip-id 41 --destination Instagram:brand --account-id 123
    python -m commercial_intelligence.reconcile ledger.json

The media check uses FFprobe to inspect the video and audio codecs, dimensions and duration. Creative review and playback are separate acceptance steps. The submission command creates a draft with the selected clip, account ID and file hash for manual verification. Reconciliation takes `earnings` and `settlements` arrays and uses recorded net payments in the stated currency.
