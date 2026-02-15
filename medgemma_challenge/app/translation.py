TRANSLATION_PREFIX = {
    "spanish": "Resumen en espanol:",
    "hindi": "Hindi summary:",
    "telugu": "Telugu summary:",
    "english": "",
}


def translate_fallback(text: str, target_language: str) -> str:
    language = str(target_language or "").strip().lower()
    if not text:
        return ""
    if language == "english":
        return text
    prefix = TRANSLATION_PREFIX.get(language, f"{language.title()} summary:")
    return f"{prefix} {text}"

