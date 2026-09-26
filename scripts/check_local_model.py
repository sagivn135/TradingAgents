"""Check the configured local server and model IDs without running an analysis."""
import json
from urllib.error import URLError
from urllib.request import urlopen

from tradingagents.default_config import DEFAULT_CONFIG


def main():
    config = DEFAULT_CONFIG
    if config["llm_provider"] != "openai_compatible":
        raise SystemExit("Set TRADINGAGENTS_LLM_PROVIDER=openai_compatible in .env")
    base = config["backend_url"]
    if not base or not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise SystemExit("This check expects a localhost LM Studio endpoint.")
    try:
        with urlopen(base.rstrip("/") + "/models", timeout=10) as response:
            models = {item["id"] for item in json.load(response)["data"]}
    except (URLError, ValueError, KeyError) as exc:
        raise SystemExit(f"LM Studio check failed: {exc}") from exc
    requested = {config["deep_think_llm"], config["quick_think_llm"]}
    missing = requested - models
    if missing:
        raise SystemExit(f"Configure/load chat model IDs in .env. Missing: {sorted(missing)}; available: {sorted(models)}")
    print("Configured model IDs are available. Tool calling and full analysis still need validation.")


if __name__ == "__main__":
    main()
