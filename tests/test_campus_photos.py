"""Regression tests: campus-specific photo retrieval, rendering payload, follow-ups.
The URLs below are TEST FIXTURES only (never shipped / never shown to real users)."""
import json, os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("SKIP_AI_STARTUP_CHECK", "1")
from chatbot import photos, pipeline

def entry(campus, n, **kw):
    e = {"campus": str(campus), "url": f"https://pcte.edu.in/test/c{campus}_{n}.jpg",
         "caption": f"Test view {n}", "source_page": f"https://pcte.edu.in/test/page{campus}",
         "verified": True, "evidence": "fixture: page caption names the campus"}
    e.update(kw); return e

def registry(c1=0, c2=0, extra=()):
    return {"allowed_hosts": ["pcte.edu.in"],
            "photos": [entry(1, i) for i in range(c1)] + [entry(2, i) for i in range(c2)] + list(extra)}

def reply(msg, reg, history=None):
    return photos.photo_reply(msg, history, registry=reg)

def as_history(msg, r):  # what the browser sends back: user text + bot text
    return [{"role": "user", "text": msg}, {"role": "bot", "text": r["text"]}]


class Classification(unittest.TestCase):
    def test_queries(self):
        c = photos.classify
        self.assertEqual(c("Show me PCTE photos")["campus"], "all")
        self.assertEqual(c("Show me Campus 1 photos")["campus"], "1")
        self.assertEqual(c("Show me Campus 2 photos")["campus"], "2")
        self.assertEqual(c("campus-2 pics please")["campus"], "2")
        self.assertEqual(c("second campus ki photos")["campus"], "2")
        self.assertEqual(c("show me pictures of campus two")["campus"], "2")

    def test_non_photo_questions_untouched(self):
        for q in ["Campus 2 address?", "BCA fees", "Which course teaches Photoshop?",
                  "passport size photo required for admission form?", "hostel near campus 2"]:
            self.assertIsNone(photos.classify(q), q)


class Retrieval(unittest.TestCase):
    def test_campus2_only_returns_campus2(self):
        r = reply("Show me Campus 2 photos", registry(5, 5))
        self.assertEqual(len(r["photos"]), 4)
        self.assertTrue(all(p["campus"] == "2" and "/c2_" in p["url"] for p in r["photos"]))
        self.assertTrue(all(p["caption"].startswith("Test view") and p["source_page"].startswith("https://") for p in r["photos"]))

    def test_campus1_only_returns_campus1(self):
        r = reply("Show me Campus 1 photos", registry(5, 5))
        self.assertTrue(r["photos"] and all(p["campus"] == "1" for p in r["photos"]))

    def test_generic_request_keeps_campus_tag_without_caption_prefix(self):
        r = reply("Show me PCTE photos", registry(2, 2))
        self.assertEqual([p["campus"] for p in r["photos"]], ["1", "1", "2", "2"])
        self.assertTrue(all(not p["caption"].startswith("Campus") for p in r["photos"]))

    def test_campus2_never_falls_back_to_campus1(self):
        r = reply("Show me Campus 2 photos", registry(c1=6, c2=0))
        self.assertEqual(r["photos"], [])
        self.assertIn("couldn't find any official PCTE photos that are verified as Campus 2", r["text"])
        self.assertEqual(r["links"][0]["url"], "https://pcte.edu.in/")
        self.assertNotIn("c1_", json.dumps(r))

    def test_fewer_than_four_returns_what_exists(self):
        self.assertEqual(len(reply("Show me Campus 2 photos", registry(0, 2))["photos"]), 2)


class VerificationRules(unittest.TestCase):
    def test_rejects_unverified_entries(self):
        bad = [entry(2, 90, verified=False), entry(2, 91, evidence=""), entry(2, 92, caption=""),
               entry(2, 93, url="http://pcte.edu.in/x.jpg"),
               entry(2, 94, url="https://evil.example.com/campus2.jpg"),
               entry(2, 95, source_page="https://evil.example.com/p"),
               entry(2, 96, url="https://pcte.edu.in.evil.com/a.jpg"),
               entry(3, 97), entry("", 98), entry("2", 0, url="https://pcte.edu.in/campus2.jpg", evidence="  ")]
        self.assertEqual(photos.valid_entries(registry(extra=bad)), {"1": [], "2": []})

    def test_filename_or_domain_alone_is_not_evidence(self):
        e = entry(2, 1, evidence=""); e["url"] = "https://pcte.edu.in/campus-2/building.jpg"
        self.assertEqual(photos.valid_entries(registry(extra=[e]))["2"], [])

    def test_duplicate_urls_collapsed(self):
        self.assertEqual(len(photos.valid_entries(registry(extra=[entry(2, 1), entry(2, 1)]))["2"]), 1)

    def test_shipped_registry_is_clean(self):
        reg = photos.load_registry()
        accepted = sum(len(v) for v in photos.valid_entries(reg).values())
        self.assertEqual(accepted, len([e for e in reg.get("photos", [])]),
                         "campus_photos.json contains entries that fail the verification rules")

    def test_missing_registry_is_safe(self):
        self.assertEqual(photos.load_registry("/nonexistent.json"), {})
        r = photos.photo_reply("Show me Campus 2 photos", None, registry={})
        self.assertEqual(r["photos"], [])


class FollowUps(unittest.TestCase):
    def test_more_campus2_continues_without_repeats(self):
        reg = registry(3, 10)
        q1 = "Show me Campus 2 photos"; r1 = reply(q1, reg)
        q2 = "Show me more Campus 2 photos"; r2 = reply(q2, reg, as_history(q1, r1))
        r3 = reply("show more", reg, as_history(q1, r1) + as_history(q2, r2))
        urls = [p["url"] for r in (r1, r2, r3) for p in r["photos"]]
        self.assertEqual(len(urls), 10); self.assertEqual(len(set(urls)), 10)
        self.assertTrue(all("/c2_" in u for u in urls))

    def test_exhausted(self):
        reg = registry(0, 4); q = "Show me Campus 2 photos"; r1 = reply(q, reg)
        r2 = reply("Show me more Campus 2 photos", reg, as_history(q, r1))
        self.assertEqual(r2["photos"], [])
        self.assertIn("all the verified Campus 2 photos", r2["text"])

    def test_bare_more_keeps_campus_and_does_not_leak_other_campus(self):
        reg = registry(10, 10); q = "Show me Campus 2 photos"; r1 = reply(q, reg)
        r2 = reply("more", reg, as_history(q, r1))
        self.assertTrue(r2["photos"] and all(p["campus"] == "2" for p in r2["photos"]))

    def test_switching_campus_restarts(self):
        reg = registry(10, 10); q = "Show me Campus 2 photos"; r1 = reply(q, reg)
        r2 = reply("Show me Campus 1 photos", reg, as_history(q, r1))
        self.assertTrue(all(p["campus"] == "1" for p in r2["photos"]))
        self.assertTrue(r2["text"].endswith("photos 1-4 of 10]"))

    def test_bare_more_without_photo_context_is_not_a_photo_request(self):
        self.assertIsNone(photos.classify("more", [{"role": "bot", "text": "BCA fee is ..."}]))


class PipelineAndApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(registry(5, 5), cls.tmp); cls.tmp.close()
        photos.REGISTRY_PATH = cls.tmp.name

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.tmp.name)

    def test_pipeline_event_carries_photos_without_calling_ai(self):
        events = list(pipeline.answer_events("Show me Campus 2 photos", [], None))
        final = events[-1]
        self.assertEqual(final["mode"], "photos"); self.assertEqual(len(final["photos"]), 4)

    def test_http_stream_and_json_endpoints(self):
        import app as appmod
        c = appmod.app.test_client()
        lines = c.post("/api/chat-stream", json={"message": "Show me Campus 2 photos", "history": []}).data.decode().strip().splitlines()
        final = json.loads(lines[-1])
        self.assertTrue(final["photos"] and all(p["campus"] == "2" for p in final["photos"]))
        d = c.post("/api/chat", json={"message": "Show me Campus 1 photos"}).get_json()
        self.assertTrue(d["photos"] and all(p["campus"] == "1" for p in d["photos"]))
        d = c.post("/api/chat", json={"message": "Show me PCTE photos"}).get_json()
        self.assertEqual(len(d["photos"]), 4)


class OwnPhotos(unittest.TestCase):
    def test_shipped_own_photos_load_and_are_campus_separated(self):
        pool = photos.valid_entries(photos.load_registry())
        self.assertEqual((len(pool["1"]), len(pool["2"])), (1, 4))
        self.assertTrue(all(p["url"].startswith("/static/campus_photos/campus2/") for p in pool["2"]))

    def test_missing_or_tricky_local_file_rejected(self):
        bad = [{"campus": "1", "url": "/static/campus_photos/nope.jpg", "caption": "x", "evidence": "y", "verified": True},
               {"campus": "1", "url": "/static/campus_photos/../app.py", "caption": "x", "evidence": "y", "verified": True}]
        self.assertEqual(photos.valid_entries({"allowed_hosts": [], "photos": bad})["1"], [])

    def test_campus2_request_returns_four_own_photos(self):
        r = photos.photo_reply("show me campus 2 photos", None)
        self.assertEqual(len(r["photos"]), 4)
        self.assertTrue(all(p["campus"] == "2" for p in r["photos"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
