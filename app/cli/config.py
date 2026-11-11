"""`researchforge config` — read-only inspection of model configuration
(RF-013). All logic lives in app.core.model_config. Never displays API
keys; makes no network calls."""
from pathlib import Path

from app.core import model_config, provider_config
from app.providers import registry


def _registry_has(pid: str) -> bool:
    return registry.has(pid)


def cmd_show(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    present = model_config.config_path(root).is_file()
    try:
        config = model_config.load_config(root)
    except model_config.ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1
    print(f"Configuration file: {model_config.CONFIG_RELPATH} "
          f"({'present' if present else 'absent — built-in defaults'})")
    print(f"Default provider: {config['default_provider']}")
    for pid, settings in config["providers"].items():
        cred = ("configured" if provider_config.credentials_configured(pid, root)
                else "not configured [WARNING]")
        enabled = "enabled" if settings.get("enabled") else "DISABLED [WARNING]"
        registered = "yes" if _registry_has(pid) else "NO [ERROR]"
        print(f"\nProvider '{pid}': {enabled}, registered={registered}, "
              f"credentials={cred}")
        print(f"  default_model: {settings.get('default_model')}")
        print(f"  base_url: {settings.get('base_url')}")
        print(f"  timeout_seconds: {settings.get('timeout_seconds')}")
        gen = settings.get("generation_defaults", {})
        gen_str = ", ".join(f"{k}={v}" for k, v in sorted(gen.items())) or "none"
        print(f"  generation_defaults: {gen_str}")
        print(f"  known_models: {', '.join(settings.get('known_models', []))}")
        print("  task overrides:")
        for task, override in settings.get("tasks", {}).items():
            model = override.get("model") or settings.get("default_model")
            source = "override" if override.get("model") else "inherit"
            print(f"    {task}: {model} ({source})")
    return 0


def cmd_validate(args, root=None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    try:
        config = model_config.load_config(root)
    except model_config.ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1
    print("Validation result: PASS")
    print(f"Providers: {', '.join(sorted(config['providers']))}")
    print(f"Default provider: {config['default_provider']}")
    for pid, settings in config["providers"].items():
        state = "enabled" if settings.get("enabled") else "disabled"
        print(f"  {pid}: {state}, default_model={settings.get('default_model')}")
    print("No network calls were made.")
    return 0


def setup(subparsers):
    parser = subparsers.add_parser(
        "config", help="inspect model/provider configuration (read-only)")
    sub = parser.add_subparsers(dest="config_command", metavar="<action>",
                                required=True)
    p_show = sub.add_parser("show", help="show effective configuration")
    p_show.set_defaults(handler=cmd_show)
    p_val = sub.add_parser("validate", help="validate configuration files")
    p_val.set_defaults(handler=cmd_validate)
