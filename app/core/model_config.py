"""Model/provider configuration (RF-013).

Format: JSON in config/models.json — stdlib `json` only. Rationale: the
project's zero-dependency policy (RF-003) rules out PyYAML; the schema is
flat and machine-written, so JSON gives identical validation with no new
packages. A missing configuration file yields documented built-in defaults;
an INVALID file raises ConfigError (never a silent fallback).

No network I/O happens anywhere in this module. Provider construction is
explicit (build_provider) and performs only a local credential check.
Credential isolation (RF-012 addendum) is preserved: credentials come
exclusively from the project-root .env via provider_config.load_credential.
"""
import json
from pathlib import Path

from app.core import provider_config
from app.providers import registry

# Importing the adapter module registers it (no network at import).
from app.providers.deepseek import PROVIDER_ID as DEEPSEEK_ID  # noqa: F401

CONFIG_RELPATH = "config/models.json"
CONFIG_VERSION = 1
ALLOWED_GENERATION_KEYS = {"temperature", "max_output_tokens"}
KNOWN_TASKS = ("paper_extraction", "literature_synthesis",
               "research_gap_analysis", "methodology_design",
               "section_drafting", "academic_review", "diagram_generation")


class ConfigError(Exception):
    """Invalid or unusable configuration (message is secret-free)."""


class DisabledProviderError(ConfigError):
    """The resolved provider is disabled in configuration."""


def default_config() -> dict:
    """Built-in defaults used when config/models.json does not exist."""
    return {
        "version": CONFIG_VERSION,
        "default_provider": DEEPSEEK_ID,
        "providers": {
            DEEPSEEK_ID: {
                "enabled": True,
                "default_model": "deepseek-flash",   # per docs, 2026-11-10
                "base_url": "https://api.deepseek.com",
                "timeout_seconds": 60.0,
                "generation_defaults": {"temperature": 0.2,
                                        "max_output_tokens": 2048},
                "known_models": ["deepseek-flash", "deepseek-v4-pro"],
                "tasks": {task: {} for task in KNOWN_TASKS},
            }
        },
    }


def config_path(root: Path) -> Path:
    return root / CONFIG_RELPATH


def load_config(root: Path) -> dict:
    path = config_path(root)
    if not path.is_file():
        return default_config()  # absent file: documented defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"configuration file invalid ({CONFIG_RELPATH}): {exc}") from exc
    return validate_config(data)


def _validate_provider(pid: str, p, problems: list) -> None:
    label = f"providers.{pid}"
    if not isinstance(p, dict):
        problems.append(f"{label} must be an object")
        return
    if not registry.has(pid):
        problems.append(
            f"provider '{pid}' has no registered adapter "
            f"(available: {', '.join(registry.ids()) or '<none>'})")
    if not isinstance(p.get("enabled"), bool):
        problems.append(f"{label}.enabled must be true or false")
    known = p.get("known_models")
    if not isinstance(known, list) or not known or \
            not all(isinstance(m, str) and m.strip() for m in known):
        problems.append(f"{label}.known_models must be a non-empty list of model ids")
    model = p.get("default_model")
    if not isinstance(model, str) or not model.strip():
        problems.append(f"{label}.default_model is missing")
    elif isinstance(known, list) and model not in known:
        problems.append(f"{label}.default_model '{model}' is not in known_models")
    base_url = p.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        problems.append(f"{label}.base_url must be an https:// URL")
    timeout = p.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or timeout <= 0:
        problems.append(f"{label}.timeout_seconds must be a positive number")
    gen = p.get("generation_defaults")
    if not isinstance(gen, dict):
        problems.append(f"{label}.generation_defaults must be an object")
    else:
        for key, value in gen.items():
            if key not in ALLOWED_GENERATION_KEYS:
                problems.append(f"{label}.generation_defaults.{key} is not a "
                                f"recognised parameter ({sorted(ALLOWED_GENERATION_KEYS)})")
            elif key == "temperature" and (not isinstance(value, (int, float))
                                           or isinstance(value, bool)
                                           or not 0.0 <= value <= 2.0):
                problems.append(f"{label}.generation_defaults.temperature "
                                "must be between 0.0 and 2.0")
            elif key == "max_output_tokens" and (not isinstance(value, int)
                                                 or isinstance(value, bool)
                                                 or value < 1):
                problems.append(f"{label}.generation_defaults.max_output_tokens "
                                "must be a positive integer")
    tasks = p.get("tasks")
    if not isinstance(tasks, dict):
        problems.append(f"{label}.tasks must be an object")
    else:
        for task, override in tasks.items():
            if not isinstance(override, dict):
                problems.append(f"{label}.tasks.{task} must be an object")
                continue
            extra = set(override) - {"model"}
            if extra:
                problems.append(f"{label}.tasks.{task} has unknown keys: "
                                f"{sorted(extra)}")
            model = override.get("model")
            if model is not None and (not isinstance(model, str)
                                      or model not in (known or [])):
                problems.append(f"{label}.tasks.{task}.model '{model}' is not "
                                f"in known_models")


def validate_config(data) -> dict:
    """Validate structure and semantics. Raises ConfigError listing every
    problem; returns the data unchanged when valid."""
    problems: list = []
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a JSON object")
    if data.get("version") != CONFIG_VERSION:
        problems.append(f"unsupported configuration version: "
                        f"{data.get('version')!r} (expected {CONFIG_VERSION})")
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        problems.append("providers must be a non-empty object")
    else:
        for pid, p in providers.items():
            _validate_provider(pid, p, problems)
        default_provider = data.get("default_provider")
        if default_provider not in (providers or {}):
            problems.append(f"default_provider {default_provider!r} is not "
                            "defined under providers")
    if problems:
        raise ConfigError("invalid configuration: " + "; ".join(problems))
    return data


def _provider_order(config: dict) -> list:
    default_provider = config["default_provider"]
    return [default_provider] + sorted(
        pid for pid in config["providers"] if pid != default_provider)


def resolve_provider_settings(config: dict, provider_id: str) -> dict:
    """Validated settings for one provider; raises for disabled/unknown."""
    validate_config(config)
    settings = config["providers"].get(provider_id)
    if settings is None:
        raise ConfigError(f"provider not configured: '{provider_id}'")
    if not settings.get("enabled", False):
        raise DisabledProviderError(f"provider '{provider_id}' is disabled "
                                    "in configuration")
    return settings


def resolve_task(config: dict, task: str) -> dict:
    """Resolve {"provider", "model"} for a task. A task listed under a
    provider inherits that provider's default_model when its override has
    no model; a task listed nowhere inherits the default provider's
    default_model. Disabled providers are rejected, never skipped."""
    validate_config(config)
    for pid in _provider_order(config):
        settings = config["providers"][pid]
        if task in settings.get("tasks", {}):
            if not settings.get("enabled", False):
                raise DisabledProviderError(
                    f"task '{task}' is configured on disabled provider "
                    f"'{pid}'")
            model = settings["tasks"][task].get("model") \
                or settings["default_model"]
            return {"provider": pid, "model": model}
    default_provider = config["default_provider"]
    settings = config["providers"][default_provider]
    if not settings.get("enabled", False):
        raise DisabledProviderError(
            f"provider '{default_provider}' is disabled in configuration")
    return {"provider": default_provider, "model": settings["default_model"]}


def apply_generation_defaults(config: dict, provider_id: str,
                              overrides: dict | None = None) -> dict:
    """Merge configured generation defaults with explicit overrides
    (overrides win). Returns kwargs suitable for ProviderRequest."""
    settings = resolve_provider_settings(config, provider_id)
    merged = dict(settings.get("generation_defaults", {}))
    for key, value in (overrides or {}).items():
        if key not in ALLOWED_GENERATION_KEYS and key != "model":
            raise ConfigError(f"unknown generation parameter: '{key}'")
        merged[key] = value
    return merged


def build_provider(config: dict, provider_id: str, root: Path):
    """Construct the provider adapter for explicit use. Credential check
    is local (.env only, RF-012); no network call happens here."""
    resolve_provider_settings(config, provider_id)
    if provider_id != DEEPSEEK_ID:
        raise ConfigError(
            f"no adapter available for provider '{provider_id}' "
            f"(only '{DEEPSEEK_ID}' is implemented; Perplexity/Gemini/"
            "ChatGPT results enter via manual source imports)")
    from app.providers.deepseek import DeepSeekProvider
    settings = config["providers"][provider_id]
    return DeepSeekProvider(
        base_url=settings.get("base_url"),
        default_model=settings.get("default_model"),
        timeout=settings.get("timeout_seconds"),
        root=root,
    )
