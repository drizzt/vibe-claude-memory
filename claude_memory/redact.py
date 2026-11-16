# SPDX-FileCopyrightText: 2026 GodCC6
# SPDX-FileCopyrightText: 2026 Timothy Redaelli
#
# SPDX-License-Identifier: MIT

"""Secret scrubbing for memory text that is about to enter model context.

Claude's memory files are working notes, and real notes do contain live
credentials (session tokens, passwords, DSNs with inline credentials). Vibe
sends assembled context to a different model provider than the one those notes
were written for, so every byte this plugin contributes passes through
``redact_text`` first.

The policy is deliberately conservative: a false positive costs one masked
line, a false negative ships a credential to a third party.
"""

import re

REDACTION_MARK = "[REDACTED]"

_I = re.IGNORECASE
# re.ASCII keeps \b and \s on ASCII word boundaries, so a key glued to CJK text
# still counts as a separate word.
_A = re.ASCII

# (kind, pattern, group, require_entropy). group 0 replaces the whole match,
# otherwise only that capture group. Python's lookbehind must be fixed width,
# so the keyword prefixes of the assignment rules are matched and kept instead.
RULES = [
    # Well-known provider key shapes. Anthropic keys also start with `sk-`, so
    # their rule runs first or the generic one would claim them.
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b", _A), 0, False),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b", _A), 0, False),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", _A), 0, False),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b", _A), 0, False),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", _A), 0, False),
    ("aws-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b", _A), 0, False),
    ("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b", _A), 0, False),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b", _A), 0, False),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b", _A), 0, False),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b", _A), 0, False),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+", _A), 0, False),
    # A bare AWS secret access key has no prefix (40 base64 chars), so a shape
    # rule would mask unrelated identifiers; the api-secret rule catches it
    # behind its `aws_secret_access_key` keyword.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", _A),
        0,
        False,
    ),
    # Authorization headers and PEM blocks (PGP armor adds " BLOCK").
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", _I | _A), 0, False),
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", _A
        ),
        0,
        False,
    ),
    # `password: hunter2` / ``pwd = `abc123` ``: value must be 8+ non-space chars
    # and contain a digit or be 12+ chars, which keeps prose like
    # "password: stored in 1password" from being masked. The keyword has no left
    # word boundary, because `_` is a word character and `DB_PASSWORD=...` would
    # slip through, and may be followed by a quote for JSON (`"password": ...`).
    (
        "password",
        re.compile(r"(?:password|passwd|pwd)[`'\"]?\s*[:=]\s*[`'\"]?([^\s`'\"\n]{8,})", _I | _A),
        1,
        True,
    ),
    # `token: ...` / `api_key = ...` / `secret: ...` with a long opaque value.
    # Same prefix and quote rules as above (`AWS_SECRET_ACCESS_KEY`, `access_token`).
    (
        "api-secret",
        re.compile(
            r"(?:token|api[_-]?key|apikey|secret(?:[_-]?key)?|access[_-]?key|client[_-]?secret)[`'\"]?\s*[:=]\s*"
            r"[`'\"]?([A-Za-z0-9._\-+/]{12,})",
            _I | _A,
        ),
        1,
        False,
    ),
    # Connection strings with inline credentials (the user may be empty, as in
    # `redis://:pass@host`).
    (
        "dsn",
        re.compile(r"\b(?:jdbc:)?[a-z][a-z0-9+.-]*://[^\s:'\"@/]*:[^\s:'\"@/]+@[^\s'\"]+", _I | _A),
        0,
        False,
    ),
    # Long opaque hex blobs (tokens, hashes used as credentials), either case.
    ("long-hex", re.compile(r"\b[0-9a-f]{32,}\b", _I | _A), 0, False),
]


def _looks_secret(value):
    """Does a candidate value look random enough to be a real secret?"""
    if len(value) >= 12:
        return True
    return re.search(r"[0-9]", value) is not None and re.search(r"[A-Za-z]", value) is not None


def redact_text(text, mode="on"):
    """Scrub credential-shaped substrings.

    ``mode`` is ``on`` (mask), ``report`` (count only, text unchanged) or ``off``.
    Returns ``(text, hits)`` with per-rule hit counts.
    """
    hits = {}
    if mode == "off" or not isinstance(text, str) or not text:
        return text, hits

    output = text
    for kind, pattern, group, require_entropy in RULES:
        count = 0

        def replace(m):
            nonlocal count
            captured = m.group(group)
            if not captured:
                return m.group(0)
            if require_entropy and not _looks_secret(captured):
                return m.group(0)
            count += 1
            if mode == "report":
                return m.group(0)
            if group == 0:
                # Keep the line count so `file:line` references after a masked
                # multi-line block (a PEM key) still point at the right line.
                return REDACTION_MARK + "\n" * m.group(0).count("\n")
            start = m.start(group) - m.start(0)
            end = m.end(group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + REDACTION_MARK + whole[end:]

        output = pattern.sub(replace, output)
        if count:
            hits[kind] = count
    return output, hits


def describe_hits(hits):
    """One-line summary of redaction hits, or ``""`` when clean."""
    return ", ".join(f"{kind}×{n}" for kind, n in hits.items())
