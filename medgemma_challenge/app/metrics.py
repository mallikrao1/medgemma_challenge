import re


def _count_syllables(word: str) -> int:
    token = re.sub(r"[^a-z]", "", (word or "").lower())
    if not token:
        return 1
    vowels = "aeiouy"
    syllables = 0
    prev_vowel = False
    for char in token:
        is_vowel = char in vowels
        if is_vowel and not prev_vowel:
            syllables += 1
        prev_vowel = is_vowel
    if token.endswith("e") and syllables > 1:
        syllables -= 1
    return max(1, syllables)


def flesch_reading_ease(text: str) -> float:
    if not text:
        return 0.0
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    words = re.findall(r"[A-Za-z']+", text)
    if not words:
        return 0.0
    syllables = sum(_count_syllables(word) for word in words)
    words_count = len(words)
    score = 206.835 - 1.015 * (words_count / sentences) - 84.6 * (syllables / words_count)
    return round(float(score), 2)

