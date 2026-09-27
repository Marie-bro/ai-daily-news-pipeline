from __future__ import annotations

"""Deterministic bilingual checks grounded in a shared, source-derived fact schema.

The checks deliberately compare facts, not English and Chinese surface wording. A
translation such as ``1.2 billion`` / ``12 亿`` or ``MIT`` / ``麻省理工学院`` is
therefore accepted when the common schema declares the two forms as one fact.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Iterable


_FIELD_NAMES = {"title", "what_happened", "why_it_matters"}
_DATE_ISO = re.compile(r"(?<!\d)(?P<year>20\d{2})[-/](?P<month>1[0-2]|0?[1-9])[-/](?P<day>3[01]|[12]\d|0?[1-9])(?!\d)")
_DATE_ZH = re.compile(
    r"(?:(?P<year>20\d{2})\s*\u5e74\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*\u6708\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*[\u65e5\u53f7]?"
)
_DATE_EN = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(?P<day>[12]?\d|3[01])(?:,?\s*(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_DATE_EN_RANGE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(?P<start>[12]?\d|3[01])\s*[-\u2013]\s*(?P<end>[12]?\d|3[01])(?:,?\s*(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_TIME = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_VERSION = re.compile(r"\bv?\d+(?:\.\d+){1,3}\b", re.IGNORECASE)
_SCALED_NUMBER_SUFFIX = re.compile(r"\s*(?:thousand|million|billion|trillion)\b", re.IGNORECASE)
_EN_NUMBER = re.compile(
    r"(?<![\w.])(?P<value>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?P<unit>%|percent|thousand|million|billion|trillion|[kKmMbB]|[xX])?(?!\w)",
    re.IGNORECASE,
)
_ZH_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>\u4e07\u4ebf|\u5343\u4e07|\u767e\u4e07|%|\uff05|\u5343|\u4e07|\u4ebf|\u500d|[kKmMbB]|[xX])?(?![A-Za-z0-9_])"
)
_EN_NUMBER_WORD = re.compile(
    r"\b(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*-?\s*(?:loops?|steps?|particles?)\b",
    re.IGNORECASE,
)
_EN_SCALED_NUMBER_WORD = re.compile(
    r"\b(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
    r"(?P<scale>thousand|million|billion|trillion)\b",
    re.IGNORECASE,
)
_ZH_CLASSIFIED_NUMBER = re.compile(
    r"(?P<value>[\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341])"
    r"(?=(?:\u5708|\u7c92\u5b50|\u7ef4|\u500d|\u6beb\u79d2|\u53c2\u6570|\u6b65))"
)
_EN_AVAILABILITY = re.compile(r"\b(?:does not support|not supported|unavailable|available|supports?|released|release|ships?|will release|planned)\b", re.IGNORECASE)
_ZH_AVAILABILITY = re.compile(
    r"(?:\u4e0d\u652f\u6301|\u672a\u652f\u6301|\u4e0d\u53ef\u7528|\u53ef\u7528|\u652f\u6301|"
    r"\u5df2\u53d1\u5e03|\u53d1\u5e03|\u5c06\u53d1\u5e03|\u8ba1\u5212\u53d1\u5e03|\u5f00\u653e|\u91ca\u653e|\u83b7\u5f97|\u83b7\u53d6)"
)
_HARD_FACT_TYPES = {
    "organization", "organisation", "company", "institution", "product", "model", "technology", "technical_name", "version",
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "\u4e00": 1, "\u4e8c": 2, "\u4e24": 2, "\u4e09": 3, "\u56db": 4, "\u4e94": 5,
    "\u516d": 6, "\u4e03": 7, "\u516b": 8, "\u4e5d": 9, "\u5341": 10,
}


@dataclass
class BilingualValidationError(ValueError):
    """A safe, structured validation failure that can drive one field-only repair."""

    stage: str
    reason: str
    failed_fields: tuple[str, ...]
    repair_fields: tuple[str, ...] = ()
    hard_fact_conflict: bool = False
    natural_translation_difference: bool = False

    def __str__(self) -> str:
        return self.reason


def _string_list(value: object, field: str, article_id: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BilingualValidationError("schema", f"Item {article_id} has an invalid fact_schema.{field}", (), (), False, False)
    return [item.strip() for item in value]


def normalize_fact_schema(value: object, article_id: str) -> dict[str, object]:
    """Validate the one shared schema without interpreting it as user-visible content."""
    if not isinstance(value, dict):
        raise BilingualValidationError("schema", f"Item {article_id} has no object fact_schema", (), (), False, False)
    if value.get("article_id") != article_id:
        raise BilingualValidationError("schema", f"Item {article_id} fact_schema.article_id does not match", (), (), False, False)
    category = value.get("category")
    if not isinstance(category, str) or not category.strip():
        raise BilingualValidationError("schema", f"Item {article_id} has an invalid fact_schema.category", (), (), False, False)
    raw_facts = value.get("core_facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise BilingualValidationError("schema", f"Item {article_id} fact_schema.core_facts must not be empty", (), (), False, False)

    facts: list[dict[str, object]] = []
    ids: set[str] = set()
    for raw_fact in raw_facts:
        if not isinstance(raw_fact, dict):
            raise BilingualValidationError("schema", f"Item {article_id} has a non-object core fact", (), (), False, False)
        fact_id = raw_fact.get("id")
        kind = raw_fact.get("type")
        canonical = raw_fact.get("value")
        rendered_in = raw_fact.get("rendered_in")
        if (not isinstance(fact_id, str) or not fact_id.strip() or fact_id in ids or not isinstance(kind, str)
                or not kind.strip() or not isinstance(canonical, str) or not canonical.strip()
                or not isinstance(rendered_in, list) or not rendered_in):
            raise BilingualValidationError("schema", f"Item {article_id} has an invalid core fact", (), (), False, False)
        rendered = [entry for entry in rendered_in if isinstance(entry, str) and entry in _FIELD_NAMES]
        if len(rendered) != len(rendered_in):
            raise BilingualValidationError("schema", f"Item {article_id} has an invalid core fact rendered_in", (), (), False, False)
        english_forms = _string_list(raw_fact.get("english_forms"), "core_facts.english_forms", article_id)
        chinese_forms = _string_list(raw_fact.get("chinese_forms"), "core_facts.chinese_forms", article_id)
        ids.add(fact_id)
        facts.append({
            "id": fact_id, "type": kind.strip(), "value": canonical.strip(), "rendered_in": rendered,
            "english_forms": english_forms, "chinese_forms": chinese_forms,
        })

    normalized: dict[str, object] = {"article_id": article_id, "category": category.strip(), "core_facts": facts}
    for field in ("key_entities", "dates", "numbers", "versions", "products", "companies", "technologies", "limitations", "importance_reasons"):
        normalized[field] = _string_list(value.get(field, []), field, article_id)
    scope = value.get("scope", "")
    if not isinstance(scope, str):
        raise BilingualValidationError("schema", f"Item {article_id} has an invalid fact_schema.scope", (), (), False, False)
    normalized["scope"] = scope.strip()
    return normalized


def _contains_any(text: str, forms: Iterable[str]) -> bool:
    folded = re.sub(r"\s+", "", text.casefold())
    return any(re.sub(r"\s+", "", form.casefold()) in folded for form in forms)


def _observed_schema_facts(text: str, facts: list[dict[str, object]], language: str) -> set[str]:
    form_key = "english_forms" if language == "en" else "chinese_forms"
    return {str(fact["id"]) for fact in facts if _contains_any(text, fact[form_key])}


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _numbers(text: str, language: str) -> set[str]:
    pattern = _EN_NUMBER if language == "en" else _ZH_NUMBER
    multipliers = {
        "": Decimal(1), "%": Decimal(1), "percent": Decimal(1), "％": Decimal(1),
        "thousand": Decimal(1_000), "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000),
        "trillion": Decimal(1_000_000_000_000), "k": Decimal(1_000), "m": Decimal(1_000_000), "b": Decimal(1_000_000_000),
        "千": Decimal(1_000), "万": Decimal(10_000),
        "亿": Decimal(100_000_000), "万亿": Decimal(1_000_000_000_000), "百万": Decimal(1_000_000), "千万": Decimal(10_000_000),
    }
    result: set[str] = set()
    for match in pattern.finditer(text):
        try:
            value = Decimal(match.group("value").replace(",", ""))
        except (InvalidOperation, TypeError):
            continue
        unit = (match.group("unit") or "").casefold()
        if unit == "\uff05":
            unit = "%"
        elif unit == "\u500d":
            unit = "x"
        if not unit and language == "en":
            following = re.match(r"\s*(thousand|million|billion|trillion)\b", text[match.end():], re.IGNORECASE)
            if following:
                unit = following.group(1).casefold()
        if unit in {"%", "percent", "％"}:
            result.add(f"percent:{_decimal_text(value)}")
        elif unit in {"x", "倍"}:
            result.add(f"multiple:{_decimal_text(value)}")
        else:
            result.add(f"number:{_decimal_text(value * multipliers[unit])}")
    if language == "en":
        for match in _EN_NUMBER_WORD.finditer(text):
            result.add(f"number:{_NUMBER_WORDS[match.group('word').casefold()]}")
        for match in _EN_SCALED_NUMBER_WORD.finditer(text):
            value = Decimal(_NUMBER_WORDS[match.group("word").casefold()])
            scale = match.group("scale").casefold()
            result.add(f"number:{_decimal_text(value * multipliers[scale])}")
    else:
        for match in _ZH_CLASSIFIED_NUMBER.finditer(text):
            value = _NUMBER_WORDS.get(match.group("value"))
            if value is not None:
                result.add(f"number:{value}")
    return result


def _dates(text: str) -> set[str]:
    found: set[str] = set()
    for match in _DATE_ISO.finditer(text):
        found.add(f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}")
    for match in _DATE_ZH.finditer(text):
        prefix = f"{match.group('year')}-" if match.group("year") else ""
        found.add(f"{prefix}{int(match.group('month')):02d}-{int(match.group('day')):02d}")
    for match in _DATE_EN_RANGE.finditer(text):
        month = _MONTHS[match.group("month").casefold().rstrip(".")]
        prefix = f"{match.group('year')}-" if match.group("year") else ""
        found.add(f"{prefix}{month:02d}-{int(match.group('start')):02d}")
        found.add(f"{prefix}{month:02d}-{int(match.group('end')):02d}")
    for match in _DATE_EN.finditer(text):
        month = _MONTHS[match.group("month").casefold().rstrip(".")]
        prefix = f"{match.group('year')}-" if match.group("year") else ""
        found.add(f"{prefix}{month:02d}-{int(match.group('day')):02d}")
    return found


def _without_dates(text: str) -> str:
    """Dates are checked separately so month notation is not double-counted as a metric."""
    text = _DATE_EN_RANGE.sub(" ", text)
    text = _DATE_ISO.sub(" ", text)
    text = _DATE_ZH.sub(" ", text)
    text = _DATE_EN.sub(" ", text)
    return _TIME.sub(" ", text)


def _times(text: str) -> set[str]:
    return set(_TIME.findall(text))


def _dates_compatible(left: set[str], right: set[str]) -> bool:
    def same(a: str, b: str) -> bool:
        return a == b or a[-5:] == b[-5:]
    return all(any(same(a, b) for b in right) for a in left) and all(any(same(b, a) for a in left) for b in right)


def _versions(text: str) -> set[str]:
    """Do not mistake a decimal metric such as 1.2 billion for a software version."""
    found: set[str] = set()
    for match in _VERSION.finditer(text):
        suffix = text[match.end():]
        if (_SCALED_NUMBER_SUFFIX.match(suffix) or suffix[:1] in {"%", "\uff05"}
                or re.match(r"\s*(?:[xX]|\u500d|\u5343|\u4e07|\u4ebf|\u4e07\u4ebf|\u767e\u4e07|\u5343\u4e07)", suffix)):
            continue
        found.add(match.group(0).casefold())
    return found


def _availability(text: str, language: str) -> set[str]:
    values = _EN_AVAILABILITY.findall(text) if language == "en" else _ZH_AVAILABILITY.findall(text)
    normalized: set[str] = set()
    for value in values:
        term = value.casefold()
        if term == "release":
            normalized.add("released")
            continue
        if language == "zh":
            if term in {"\u4e0d\u652f\u6301", "\u672a\u652f\u6301", "\u4e0d\u53ef\u7528"}:
                normalized.add("unsupported")
            elif term in {"\u5c06\u53d1\u5e03", "\u8ba1\u5212\u53d1\u5e03"}:
                normalized.add("planned")
            elif term in {"\u5df2\u53d1\u5e03", "\u53d1\u5e03", "\u91ca\u653e"}:
                normalized.add("released")
            else:
                normalized.add("supported")
            continue
        if term in {"does not support", "not supported", "unavailable", "不支持", "未支持", "不可用"}:
            normalized.add("unsupported")
        elif term in {"will release", "planned", "将发布", "计划发布"}:
            normalized.add("planned")
        elif term in {"released", "ships", "ship", "已发布", "发布"}:
            normalized.add("released")
        else:
            normalized.add("supported")
    return normalized


def _repair_field(field: str, language: str = "zh") -> str:
    return {"title": {"en": "title_en", "zh": "title_cn"}, "what_happened": {"en": "what_happened_en", "zh": "what_happened"}, "why_it_matters": {"en": "why_it_matters_en", "zh": "why_it_matters"}}[field][language]


def _hard_error(field: str, reason: str, *, language: str = "zh") -> BilingualValidationError:
    return BilingualValidationError("hard_facts", f"{field}: {reason}", (field,), (_repair_field(field, language),), True, False)


def validate_shared_fact_consistency(english: str, chinese: str, field: str, fact_schema: dict[str, object]) -> None:
    """Validate fact equivalence while allowing ordinary, natural translation differences."""
    if field not in _FIELD_NAMES:
        raise ValueError(f"Unsupported bilingual field: {field}")
    facts = fact_schema["core_facts"]
    if not isinstance(facts, list):
        raise ValueError("fact_schema.core_facts is invalid")
    facts = [
        fact for fact in facts
        if str(fact["type"]).casefold() in _HARD_FACT_TYPES
        # A sentence labelled "technology" is a semantic claim, not an entity name.
        # It remains grounded in the shared schema, while code compares concise names.
        and all(len(form) <= 48 for form in [*fact["english_forms"], *fact["chinese_forms"]])
    ]
    en_seen = _observed_schema_facts(english, facts, "en")
    zh_seen = _observed_schema_facts(chinese, facts, "zh")
    if en_seen != zh_seen:
        # rendered_in is provenance for the generation prompt, not a demand that a
        # fact be repeated in every short field. Reject only facts stated in one
        # language but absent from its paired natural rendering.
        raise _hard_error(field, "English and Chinese mention different shared facts")

    en_numbers, zh_numbers = _numbers(_without_dates(english), "en"), _numbers(_without_dates(chinese), "zh")
    if en_numbers != zh_numbers:
        raise _hard_error(field, f"numbers differ ({sorted(en_numbers)} vs {sorted(zh_numbers)})")
    en_versions = _versions(english)
    zh_versions = _versions(chinese)
    if en_versions != zh_versions:
        raise _hard_error(field, f"versions differ ({sorted(en_versions)} vs {sorted(zh_versions)})")
    en_dates, zh_dates = _dates(english), _dates(chinese)
    if not _dates_compatible(en_dates, zh_dates):
        raise _hard_error(field, f"dates differ ({sorted(en_dates)} vs {sorted(zh_dates)})")
    if _times(english) != _times(chinese):
        raise _hard_error(field, f"times differ ({sorted(_times(english))} vs {sorted(_times(chinese))})")
    en_availability, zh_availability = _availability(english, "en"), _availability(chinese, "zh")
    if en_availability and zh_availability and en_availability != zh_availability:
        raise _hard_error(field, f"availability or release intent differs ({sorted(en_availability)} vs {sorted(zh_availability)})")


def validate_semantic_consistency(english: str, chinese: str, field: str, fact_schema: dict[str, object]) -> None:
    """Schema-provenance semantic check; it intentionally does not compare word order or length."""
    if not english.strip() or not chinese.strip():
        raise BilingualValidationError("semantic", f"{field}: a bilingual rendering is empty", (field,), (), False, False)
    validate_shared_fact_consistency(english, chinese, field, fact_schema)


def validate_literal_consistency(english: str, chinese: str, field: str) -> None:
    """Legacy/backfill guard retaining only language-independent literal checks."""
    if _numbers(_without_dates(english), "en") != _numbers(_without_dates(chinese), "zh"):
        raise ValueError(f"{field} has different numbers")
    if _versions(english) != _versions(chinese):
        raise ValueError(f"{field} has different versions")
    if not _dates_compatible(_dates(english), _dates(chinese)):
        raise ValueError(f"{field} has different dates")
