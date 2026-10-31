"""
admission_faq.py - instant answer for "what is required for admission?" style questions.

No AI, no database needed. Understands spelling mistakes (admmision, addmission, admision ...).
Questions about FEES / hostel / bus / placements are NOT taken here - they go to the normal flow.

>>> EDIT THE TEXT BELOW if the college's official admission page says something different. <<<
"""
import difflib
import re

CONTACT = "+91-161-2888500 / admissions@pcte.edu.in"

ANSWER = (
    "Admission at PCTE - general requirements:\n"
    "\n"
    "Eligibility\n"
    "\u2022 10+2 (or equivalent) pass from a recognised board.\n"
    "\u2022 Admission to most UG courses (BCA, BBA, B.Com etc.) is merit-based. "
    "Some courses may also have counselling / interview.\n"
    "\n"
    "Documents needed\n"
    "\u2022 10th and 12th mark sheets\n"
    "\u2022 Transfer certificate (if required)\n"
    "\u2022 Character certificate\n"
    "\u2022 Passport-size photographs\n"
    "\u2022 Any other document asked by the college\n"
    "\n"
    "Process\n"
    "\u2022 Fill the application form \u2192 merit list / shortlisting \u2192 document verification \u2192 fee payment.\n"
    "\n"
    "Exact eligibility, dates and seats can differ by course, so please confirm with the admissions office: "
    + CONTACT
)

_ADMISSION = "admission"
_OTHER_TOPIC = {"fee", "fees", "fees.", "hostel", "bus", "placement", "placements", "package", "canteen",
                "library", "wifi", "timing", "timings", "holiday", "holidays", "scholarship", "scholarships"}
_PROCESS_WORDS = {"apply", "eligibility", "eligible", "criteria", "requirement", "requirements", "required"}


def _tokens(text):
    return re.findall(r"[a-z]+", (text or "").lower())


def _is_admission_word(tok):
    return len(tok) >= 5 and difflib.SequenceMatcher(None, tok, _ADMISSION).ratio() >= 0.8


def is_admission_question(message):
    toks = _tokens(message)
    if not toks or set(toks) & _OTHER_TOPIC:
        return False
    if any(_is_admission_word(t) for t in toks):
        return True
    # "what are the eligibility criteria for pcte" / "how to apply"
    return bool(set(toks) & _PROCESS_WORDS) and "pcte" in toks


def admission_reply(message):
    return ANSWER if is_admission_question(message) else None
