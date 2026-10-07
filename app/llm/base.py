"""LLMClient factory: the only place that knows which provider adapters exist."""
from app.config import Settings
from app.llm.types import LLMClient


def make_llm(settings: Settings, *, fast: bool = False) -> LLMClient:
    """The configured model; `fast` is the cheap one for background chores (LLM_FAST_MODEL)."""
    model = (fast and settings.llm_fast_model) or settings.llm_model
    # Adapters import their vendor SDK lazily, so only the configured extra must be installed.
    if settings.llm_provider in ("claude_code", "codex_cli"):
        # A subscription through the vendor's own CLI: its sign-in, so no key and no endpoint.
        from app.llm.claude_code import ClaudeCodeClient
        from app.llm.codex_cli import CodexCliClient

        cli_class = ClaudeCodeClient if settings.llm_provider == "claude_code" else CodexCliClient
        return cli_class(model=model, cli_path=settings.llm_cli_path, timeout=settings.llm_cli_timeout,
                         supports_images=settings.llm_supports_images)
    if settings.llm_provider == "anthropic":
        from app.llm.anthropic import AnthropicClient

        return AnthropicClient(
            api_key=settings.llm_api_key, model=model,
            base_url=settings.llm_base_url, supports_images=settings.llm_supports_images,
        )
    from app.llm.openai_compat import OpenAICompatClient

    return OpenAICompatClient(
        api_key=settings.llm_api_key, model=model,
        base_url=settings.llm_base_url, supports_images=settings.llm_supports_images,
    )
