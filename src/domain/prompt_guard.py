"""
Agentic Security Gate against Indirect Prompt Injection (OWASP LLM01).
Sanitizes untrusted free-text fields stored in database records (comments,
plot names, farmer names, locations) before exposing them to AI agents.
"""
import re
from typing import Tuple, Dict, Any, List

INJECTION_RULES = [
    ("OWASP_LLM01_INSTRUCTION_OVERRIDE_EN", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE)),
    ("OWASP_LLM01_INSTRUCTION_OVERRIDE_ES", re.compile(r"ignora\s+(todas\s+)?las\s+instrucciones\s+(anteriores|previas)", re.IGNORECASE)),
    ("OWASP_LLM01_ROLE_MARKER", re.compile(r"<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]|<system>|</system>|system\s*prompt|system\s*instruction|you\s+are\s+now", re.IGNORECASE)),
    ("OWASP_LLM01_DOMAIN_TAMPERING", re.compile(r"override\s+eudr|force\s+compliant|release_container_green_channel", re.IGNORECASE)),
]

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_untrusted_text(value: Any, max_length: int = 256) -> Tuple[str, List[str]]:
    """
    Cleans untrusted database text and neutralizes prompt injection attempts.
    Returns: (sanitized_string, list_of_detected_threat_codes)
    """
    if value is None:
        return "", []

    text = CONTROL_CHARS_RE.sub("", str(value)).strip()
    threats: List[str] = []

    for code, pattern in INJECTION_RULES:
        if pattern.search(text):
            threats.append(code)
            text = pattern.sub("[REDACTED_PROMPT_INJECTION]", text)

    if len(text) > max_length:
        text = text[:max_length] + "..."

    return text, threats


def sanitize_record_fields(record: Dict[str, Any], fields: List[str], max_length: int = 256) -> Tuple[Dict[str, Any], List[str]]:
    """
    Sanitizes specified string fields in a dictionary and returns a tuple of:
    (sanitized_record_copy, list_of_detected_threats)
    """
    cleaned = dict(record)
    all_threats: List[str] = []
    for f in fields:
        if f in cleaned and cleaned[f] is not None:
            sanitized_val, threats = sanitize_untrusted_text(cleaned[f], max_length=max_length)
            cleaned[f] = sanitized_val
            for t in threats:
                all_threats.append(f"{f}:{t}")
    return cleaned, all_threats
