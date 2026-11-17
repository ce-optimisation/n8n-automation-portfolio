"""Portfolio maintenance without any model usage.

  python scripts/portfolio_admin.py seed      add missing brands/creators/sources (idempotent; never re-enables or overwrites)
  python scripts/portfolio_admin.py verify    resolve identities through the official APIs (needs the free keys in .env)
  python scripts/portfolio_admin.py check     run one discovery pass now (reads platforms; downloads nothing)
  python scripts/portfolio_admin.py status    same as: python -m clipengine --portfolio   (read-only)

Run from the engine folder (the installed copy uses its own .venv). Secrets are never printed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipengine import discovery, events, portfolio, portfolio_report  # noqa: E402
from clipengine.config import get_settings, load_dotenv  # noqa: E402
from clipengine.db import Database  # noqa: E402


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    load_dotenv()
    s = get_settings()
    if cmd == "status":
        return portfolio_report.main(s)
    db = Database(s.db_path)
    if cmd == "seed":
        print(portfolio.seed(db, s), "events added:", events.seed_events(db))
    elif cmd == "verify":
        for r in portfolio.verify_all(s, db):
            print({k: v for k, v in r.items() if k != "evidence"}, (r.get("evidence") or "")[:90])
    elif cmd == "check":
        print(discovery.DiscoverySupervisor(s, db, None).tick())
    else:
        print(__doc__)
        return 2
    return portfolio_report.main(s)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
