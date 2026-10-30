"""
links.py - action buttons shown under an answer.

Admission questions ("admission kaise le", "apply form", "how to apply") get two buttons:
  * Apply Now            -> the online application form
  * PCTE official website
URLs come from college_config.json ("apply_url", "official_site"), never from the AI.
"""
import re

from chatbot.database import load_config

_ADMISSION_WORDS = {"admission", "admissions", "admision", "admisson", "apply", "applying", "application",
                    "enroll", "enrol", "enrolment", "enrollment", "daakhla", "dakhla", "registration", "register"}
_DEFAULT_APPLY = "https://admissions.pcte.edu.in/"
_DEFAULT_SITE = "https://pcte.edu.in/"


def is_admission_query(message):
    toks = set(re.findall(r"[a-z]+", (message or "").lower()))
    if toks & _ADMISSION_WORDS:
        return True
    low = (message or "").lower()
    return "apply form" in low or "admission form" in low or "form bhar" in low


def admission_links(message):
    """[] for normal questions, else the Apply Now + official website buttons."""
    if not is_admission_query(message):
        return []
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    apply_url = cfg.get("apply_url") or _DEFAULT_APPLY
    site = cfg.get("official_site") or _DEFAULT_SITE
    if not site.endswith("/"):
        site += "/"
    return [{"title": "Apply Now (admission form)", "url": apply_url},
            {"title": "PCTE official website", "url": site}]
