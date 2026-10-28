"""
smalltalk.py
Instant, friendly replies for greetings / thanks / goodbye / "how are you".

These never need the AI model or the brochure, so they are answered right
away (fast, free, and always the same warm tone). Anything that is a real
question (even "hello, BCA fee kitni hai?") returns None and goes through
the normal brochure-based answer flow.
"""

import re

_HELLO_TOKEN = re.compile(
    r"h+i+|h+e+l+l*o+w?|h+l+o+|h+e+y+a?|hola|hy|hye|yo|ssa|gm"
)
_FILLER = {
    "ji", "there", "sir", "madam", "mam", "maam", "bot", "dear", "friend",
    "bhai", "everyone", "all", "assistant", "campus", "pcte", "team", "please", "pls",
}
_HOW_ARE_YOU = {
    "how are you", "how are u", "how r u", "how r you", "how do you do",
    "kaise ho", "kese ho", "kaisi ho", "kaise hain", "aap kaise ho", "aap kaise hain",
    "kya haal hai", "kya haal", "whats up", "what is up", "sup",
}
_THANKS_WORDS = {
    "thanks", "thank", "thankyou", "thx", "ty", "shukriya", "shukriyaa",
    "dhanyavad", "dhanyawad", "dhanyvaad",
}
_THANKS_EXTRA = {"you", "so", "much", "a", "lot", "bahut", "ji", "ok", "okay", "again", "u", "very"}
_BYE_WORDS = {"bye", "byee", "goodbye", "tata", "alvida", "later", "cya"}


def _normalize(text):
    text = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def smalltalk_reply(message):
    """Returns a friendly reply string, or None if this is a real question."""
    text = _normalize(message)
    if not text:
        return None
    tokens = text.split()
    if len(tokens) > 7:
        return None

    # --- Thank you ---------------------------------------------------------
    if any(t in _THANKS_WORDS for t in tokens) and all(
        t in _THANKS_WORDS or t in _THANKS_EXTRA for t in tokens
    ):
        return ("You're most welcome! 😊 If you have any other question, just ask. "
                "I'm right here.")

    # --- Goodbye / good night ----------------------------------------------
    if "good night" in text or "goodnight" in text or text in ("gn", "good nite"):
        return "Good night! 🌙 Take care. Come back anytime you have a question 😊"
    if (any(t in _BYE_WORDS for t in tokens) or text in ("see you", "see you later", "take care")) and len(tokens) <= 3:
        return "Goodbye! 👋 Take care. Come back anytime you have a question 😊"

    # --- Greetings (optionally followed by "how are you") ------------------
    # Time-of-day / regional greetings first so the reply matches them.
    opener = None
    rest_text = text
    for phrase, label in (
        ("good morning", "Good morning! ☀️"),
        ("good afternoon", "Good afternoon! 🌤️"),
        ("good evening", "Good evening! 🌆"),
        ("sat sri akal", "Sat Sri Akal! 🙏"),
        ("namaste", "Namaste! 🙏"),
        ("namaskar", "Namaskar! 🙏"),
        ("pranam", "Pranam! 🙏"),
    ):
        if text.startswith(phrase):
            opener = label
            rest_text = text[len(phrase):].strip()
            break

    rest_tokens = rest_text.split()
    saw_hello = opener is not None
    # Strip any leading hello-words / filler words (e.g. "hello hi ji").
    while rest_tokens and (_HELLO_TOKEN.fullmatch(rest_tokens[0]) or rest_tokens[0] in _FILLER):
        if _HELLO_TOKEN.fullmatch(rest_tokens[0]):
            saw_hello = True
        rest_tokens.pop(0)

    if not saw_hello:
        # No greeting at the start; allow plain "how are you?" on its own.
        if text in _HOW_ARE_YOU:
            return ("I'm doing great, thank you for asking! 😊 "
                    "How can I help you?")
        return None

    rest = " ".join(rest_tokens)
    greeting = opener or "Hello! 😊"

    if not rest:
        return f"{greeting} May I help you?"
    if rest in _HOW_ARE_YOU:
        return (f"{greeting} I'm doing great, thank you for asking! "
                "May I help you?")

    # Greeting followed by a real question -> let the normal flow answer it.
    return None
