"""Provider configuration contract (RF-011, isolation per RF-012).

Credentials are read EXCLUSIVELY from the project-root .env file
(RF-003 convention, safe stdlib parser below). Global environment
variables are deliberately ignored so a machine-wide/company key can
never leak into ResearchForge. Values are never returned to callers
outside adapters — only presence/absence. Only DeepSeek is an approved
programmatic provider; Gemini/Perplexity/ChatGPT outputs arrive via the
manual source-import workflow (RF-010) and are NOT API integrations.
"""
import os
from pathlib import Path

# provider_id -> credential environment variable names.
# "gemini" is listed only as an optional/future placeholder (commented in
# .env.example); Perplexity and ChatGPT have no planned API credentials.
CREDENTIAL_ENV_VARS = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),  # optional / future
}

PLACEHOLDER_PREFIXES = ("your-", "example", "changeme")


def credential_env_vars(provider_id: str) -> tuple:
    return CREDENTIAL_ENV_VARS.get(provider_id, ())


def _read_env_file(root: Path) -> dict:
    """Parse local .env into a dict without exposing values upstream."""
    values = {}
    try:
        text = (root / ".env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return values  # missing/unreadable/binary: treated as no credentials
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not key.strip() or any(c.isspace() for c in key.strip()):
            continue  # malformed key line: skipped safely
        values[key.strip()] = value.strip()
    return values


def _is_real_value(value: str) -> bool:
    return bool(value) and not value.lower().startswith(PLACEHOLDER_PREFIXES)


def credentials_configured(provider_id: str, root: Path | None = None) -> bool:
    """True when at least one credential variable holds a non-placeholder
    value (environment first, then local .env). Never returns the value."""
    return load_credential(provider_id, root) is not None


def load_credential(provider_id: str, root: Path | None = None) -> str | None:
    """Return the credential value for internal use by adapters only
    (HTTP auth header). Never logged, never printed, never embedded in
    errors. Returns None when unset/placeholder/unreadable.

    RF-012 credential isolation: the value is read EXCLUSIVELY from the
    project-root .env file. Global environment variables are deliberately
    ignored so a machine-wide or company key can never be picked up.
    `root` (the project root) is the only permitted credential location;
    without it, no credential can be resolved.
    """
    env_names = credential_env_vars(provider_id)
    if not env_names or root is None:
        return None
    file_values = _read_env_file(root)
    for name in env_names:
        if _is_real_value(file_values.get(name, "")):
            return file_values[name]
    return None
