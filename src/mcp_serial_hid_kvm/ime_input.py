"""Type Japanese and Chinese through the Target's Microsoft IME, as a person would.

No clipboard, PowerShell or Base64 is involved: every character is plain HID
keystrokes, so it also works where only the keyboard reaches (RDP, VMs).

- ASCII is typed with the IME off.
- Hiragana is typed in romaji, one fixed spelling per character, and committed
  unconverted with Enter. Katakana is typed the same way and turned into
  katakana with F7 before Enter.
- Every other character (kanji, simplified Chinese, full-width punctuation,
  symbols) uses Microsoft IME code conversion: its 4-digit Unicode hex is typed
  in upper case, F5 converts it, Enter commits the first candidate.

Results are deterministic but not verified here. Characters missing from the
Target font may render as boxes even though the right code point was typed.
Requires Microsoft IME in romaji input mode (the default).
"""

import asyncio

from mcp.types import Tool

IME_ON = "0x90"   # HID LANG1: Japanese IME on (hiragana)
IME_OFF = "0x91"  # HID LANG2: Japanese IME off (half-width alphanumeric)

# One complete romaji spelling per hiragana. Each spelling resolves on its own
# (it ends in a vowel, "nn" or "-"), so neighbours never merge, e.g. ん+あ.
_HIRAGANA_ROMAJI = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "wo", "ん": "nn",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "di", "づ": "du", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "xa", "ぃ": "xi", "ぅ": "xu", "ぇ": "xe", "ぉ": "xo",
    "ゃ": "xya", "ゅ": "xyu", "ょ": "xyo", "っ": "xtu", "ゎ": "xwa",
    "ゔ": "vu", "ー": "-",
}

# Romaji runs are committed in chunks to keep the IME composition short.
_KANA_CHUNK = 20
MAX_TEXT_CHARS = 2000


def _katakana_as_hiragana(ch: str) -> str | None:
    """The hiragana whose romaji + F7 yields katakana *ch*, if there is one."""
    if ch == "ー":
        return ch
    if "ァ" <= ch <= "ヴ":
        hira = chr(ord(ch) - 0x60)
        if hira in _HIRAGANA_ROMAJI:
            return hira
    return None


def validate(text) -> None:
    """Reject anything this method cannot type, before any input is sent."""
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text is longer than {MAX_TEXT_CHARS} characters")
    for ch in text:
        code = ord(ch)
        if ch in "\n\t" or 0x20 <= code <= 0x7E:
            continue
        if code < 0xA0 or code == 0x7F:
            raise ValueError(f"control character U+{code:04X} cannot be typed")
        if code > 0xFFFF or 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"U+{code:04X} is outside the BMP; IME code input takes 4 hex digits")


def plan(text: str, language: str) -> list[tuple[str, str]]:
    """Split *text* into typing segments.

    Returns ``(kind, payload)`` tuples: ``ascii`` (typed raw with the IME off),
    ``hiragana`` / ``katakana`` (romaji typed with the IME on) and ``code``
    (4 upper-case hex digits for F5 conversion). *language* is ``japanese``
    (kana typed in romaji) or ``chinese`` (every non-ASCII character by code).
    """
    validate(text)
    if language not in ("japanese", "chinese"):
        raise ValueError(f"unknown language: {language}")
    segments: list[list] = []  # [kind, [one part per character]]
    for ch in text:
        last = segments[-1] if segments else None
        if ch in "\n\t" or 0x20 <= ord(ch) <= 0x7E:
            kind, part = "ascii", ch
        elif (language == "japanese" and ch == "ー"
              and last and last[0] in ("hiragana", "katakana")):
            kind, part = last[0], "-"  # the long vowel mark extends either kana run
        elif language == "japanese" and ch in _HIRAGANA_ROMAJI:
            kind, part = "hiragana", _HIRAGANA_ROMAJI[ch]
        elif language == "japanese" and _katakana_as_hiragana(ch):
            kind, part = "katakana", _HIRAGANA_ROMAJI[_katakana_as_hiragana(ch)]
        else:
            kind, part = "code", f"{ord(ch):04X}"
        if (last and last[0] == kind and kind != "code"
                and (kind == "ascii" or len(last[1]) < _KANA_CHUNK)):
            last[1].append(part)
        else:
            segments.append([kind, [part]])
    return [(kind, "".join(parts)) for kind, parts in segments]


def summary(segments) -> dict:
    """Counts for dry runs and results."""
    counts = {"ascii_chars": 0, "kana_runs": 0, "code_conversions": 0}
    for kind, payload in segments:
        if kind == "ascii":
            counts["ascii_chars"] += len(payload)
        elif kind == "code":
            counts["code_conversions"] += 1
        else:
            counts["kana_runs"] += 1
    return {**counts, "segments": len(segments)}


async def send(client, segments, *, convert_s: float, guard=lambda: None,
               sleep=None) -> None:
    """Type planned segments. *guard* runs before every HID request.

    Ends with the IME off. A transport failure part-way can leave a partial
    composition; the caller reports unknown delivery and must observe.
    """
    sleep = sleep or asyncio.sleep
    ime_on = None

    def key(name):
        guard()
        client.send_key(name)

    async def set_ime(on):
        nonlocal ime_on
        if ime_on is not on:
            key(IME_ON if on else IME_OFF)
            ime_on = on
            await sleep(0.15)

    for kind, payload in segments:
        if kind == "ascii":
            await set_ime(False)
            guard()
            client.type_text(payload, raw=True)
            continue
        await set_ime(True)
        guard()
        client.type_text(payload, raw=True)
        if kind == "katakana":
            key("f7")
            await sleep(convert_s)
        elif kind == "code":
            key("f5")
            await sleep(convert_s)
        key("enter")
        await sleep(0.05)
    await set_ime(False)


def ime_tools() -> list[Tool]:
    def tool(name, language, description):
        return Tool(name=name, description=description, inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": f"{language} text to type exactly (newlines press Enter). "
                                        f"Up to {MAX_TEXT_CHARS} characters; BMP only."},
                "dry_run": {"type": "boolean",
                            "description": "Only return the typing plan counts; no input (default false)."},
            },
            "required": ["text"],
        })

    common = (" Every character is typed as HID keystrokes through the Target's Microsoft IME"
              " (romaji input mode); no clipboard or PowerShell. Non-kana characters use IME code"
              " conversion (Unicode hex + F5), so results are exact but not verified: inspect the"
              " screenshot. Glyphs missing from the Target font may show as boxes. Ends with the IME off.")
    return [
        tool("type_japanese", "Japanese",
             "Type Japanese (kanji, kana, punctuation, ASCII) into the focused field." + common
             + " Hiragana is typed in romaji, katakana in romaji + F7."),
        tool("type_chinese", "Chinese",
             "Type Chinese (simplified or traditional, full-width punctuation, ASCII) into the focused field."
             + common),
    ]
