# Local agency CRM

Run from the Clipping workspace with Python 3.10 or later. Choose a private test directory and an explicit database path for every command.

    python -m commercial_agency.crm --db ./agency-test.sqlite init
    python -m commercial_agency.crm --db ./agency-test.sqlite add "Fixture Studio" https://example.test/about
    python -m commercial_agency.crm --db ./agency-test.sqlite stage 1 qualified
    python -m commercial_agency.crm --db ./agency-test.sqlite contact 1 https://example.test/contact "Fixture contact basis"
    python -m commercial_agency.crm --db ./agency-test.sqlite approve 1 "Fixture approval reference"
    python -m commercial_agency.crm --db ./agency-test.sqlite show 1
    python -m commercial_agency.crm --db ./agency-test.sqlite suppress 1 "Fixture opt-out"
    python -m commercial_agency.crm --db ./agency-test.sqlite export
    python -m unittest commercial_agency.test_crm -v

The gate requires qualification, a public contact page, a recorded contact basis and an approval reference. It is a local record of operator-entered checks. The tool does not validate compliance or perform outreach. Opt-out persists and revokes approval, with no removal command. A changed contact route, basis or stage invalidates an existing approval. The CRM prevents a second record with the same case-folded organisation name and normalised whitespace. An operator still needs to recognise aliases.

Use an environment with appropriate local access controls before handling business records. Keep the SQLite file, its backups and JSON exports out of source control. Store only the references needed to document approval, basis and suppression. No real contacts are bundled.

### Evidence-based prospect ranking

Use the same five criteria as the agency preparation report. Record 0, 1 or 2 points for each criterion. A positive score needs a source reference or a dated reviewer note. Use 0 and null if evidence is unavailable. Rescore when the evidence changes.

    {
      "repeatable_source": {"points": 2, "evidence": "fixture:release schedule"},
      "offer_fit": {"points": 1, "evidence": "fixture:clip requirements"},
      "editorial_fit": {"points": 1, "evidence": "fixture:subject fit"},
      "corporate_route": {"points": 1, "evidence": "fixture:verified contact page"},
      "editing_gap": {"points": 0, "evidence": null}
    }

Save the rubric as a local JSON file. Run init once again on an existing agency database to create the scoring table, then run:

    python -m commercial_agency.crm --db ./agency-test.sqlite score 1 ./criteria.json
    python -m commercial_agency.crm --db ./agency-test.sqlite rank

The ranking includes researched and qualified prospects sorted by decreasing score. It excludes opted-out and closed prospects. The full export preserves suppression and recorded scores. Scores aid prioritisation and do not change anyone’s outreach gate.

## Synthetic client onboarding and private handoff

Use an existing private scratch folder on a local filesystem. On non-Windows systems the filesystem must support hard links. The following creates only fixture workspaces and handoffs. After onboarding, place a synthetic file named clip.mp4 in the workspace inbox for the example.

    python -m commercial_agency.handoff --root ./agency-fixtures onboard fixture-one "Fixture agreement"
    python -m commercial_agency.handoff --root ./agency-fixtures package fixture-one delivery-one --rights-reference "Fixture source" --approval-reference "Fixture approval" clip.mp4
    python -m commercial_agency.handoff --root ./agency-fixtures verify fixture-one delivery-one
    python -m unittest commercial_agency.test_handoff -v

Onboarding creates a private-directory layout with a client identity file, inbox and exports directory. Packaging takes flat filenames from the selected inbox, rejects redirected paths, includes rights and approval references, and writes a manifest with size and SHA-256 for each asset. Verification checks the workspace and delivery identity, exact asset list and hashes. A completed package cannot be overwritten by this tool. The archive appears only after it is written and verified. There is no network transfer.

These controls separate workspace paths within the operator workflow. Filesystem permissions, encryption, identity-based access and authenticated client delivery require separate configuration and validation before using real client data. A hash manifest detects accidental inconsistencies, with no cryptographic signature or authenticity guarantee for a package edited by someone with write access. Enter reference IDs instead of sensitive contract text.
