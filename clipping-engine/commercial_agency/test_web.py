import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
from starlette.templating import Jinja2Templates

from commercial_agency import crm, web


class AgencyWebTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.db_path = self.data_dir / "commercial_agency" / "agency.sqlite3"
        templates = Jinja2Templates(directory=str(Path(__file__).parent))
        templates.env.loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(Path(__file__).parent),
            FileSystemLoader(Path(__file__).resolve().parents[1] / 'clipengine/web/templates'),
        ])
        app = FastAPI()

        def page(request, name, **context):
            return templates.TemplateResponse(request, name, context)

        web.register(app, SimpleNamespace(data_dir=self.data_dir), page)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_explicit_import_ranked_view_and_export(self):
        self.assertIn("The register is empty", self.client.get("/agency").text)
        self.assertEqual(self.client.get("/agency/export").json(), [])
        self.assertFalse(self.db_path.exists())

        record = {
            "organization": "<script>Example</script>",
            "source_url": "https://example.com/series",
            "next_action": "Review recurring episodes.",
            "criteria": {criterion: {"points": 0, "evidence": None} for criterion in crm.SCORE_CRITERIA},
        }
        form = {"research": json.dumps([record])}
        response = self.client.post("/agency/import", data=form)
        self.assertIn("1 added. 0 already in the register.", response.text)
        self.assertIn("&lt;script&gt;Example&lt;/script&gt;", response.text)
        self.assertIn("Outreach blocked", response.text)
        self.assertTrue(self.db_path.is_file())
        self.assertIn("0 added. 1 already in the register.", self.client.post("/agency/import", data=form).text)
        export = self.client.get("/agency/export")
        self.assertEqual(export.headers["cache-control"], "no-store")
        self.assertEqual(len(export.json()), 1)
        self.assertFalse(export.json()[0]["outreach_gate"]["ready"])

    def test_malformed_import_does_not_create_store(self):
        response = self.client.post("/agency/import", data={"research": "["})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.db_path.exists())
