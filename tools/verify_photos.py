"""Checks every entry in campus_photos.json: passes the rules AND the image really loads.
Run on a machine with internet:  python tools/verify_photos.py
Exit code 1 if any entry fails."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from chatbot import photos

reg = photos.load_registry()
ok = photos.valid_entries(reg)
accepted = {e["url"] for v in ok.values() for e in v}
bad = 0
for e in reg.get("photos", []):
    url = e.get("url", "")
    if url not in accepted:
        print(f"REJECTED (rules)  campus={e.get('campus')} {url}"); bad += 1; continue
    try:
        r = requests.get(url, timeout=15, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        ctype = r.headers.get("content-type", "")
        good = r.status_code == 200 and ctype.startswith("image/")
    except requests.RequestException as ex:
        good, ctype, r = False, str(ex), None
    print(("OK       " if good else "BROKEN   ") + f"campus={e['campus']} {url} ({ctype})")
    bad += 0 if good else 1
print(f"\nAccepted: Campus 1={len(ok['1'])}, Campus 2={len(ok['2'])}; problems={bad}")
sys.exit(1 if bad else 0)
