"""AgentRuntime factory: the plain loop by default, Letta behind `AGENT_RUNTIME=letta`."""
from app.agent.base import AgentRuntime
from app.agent.loop import LoopRuntime
from app.config import Settings
from app.llm.base import make_llm
from app.media.store import MediaStore


def make_runtime(settings: Settings, media: MediaStore | None, *, tool_url: str | None = None) -> AgentRuntime:
    """`tool_url` is where Letta's tools reach the process that will run the turns."""
    if settings.agent_runtime == "letta":
        # letta-client is an optional extra, imported only when the runtime is switched on.
        from letta_client import AsyncLetta

        from app.agent.letta_runtime import LettaRuntime

        if not (settings.letta_base_url and settings.internal_tool_token):
            raise ValueError("AGENT_RUNTIME=letta needs LETTA_BASE_URL and INTERNAL_TOOL_TOKEN")
        return LettaRuntime(
            AsyncLetta(base_url=settings.letta_base_url, api_key=settings.letta_api_key),
            tool_url=tool_url or settings.internal_base_url or settings.public_base_url,
            tool_token=settings.internal_tool_token, model=settings.letta_model, media=media,
            agent_name=settings.agent_name, max_iterations=settings.llm_max_tool_iterations,
            supports_images=settings.llm_supports_images)
    return LoopRuntime(make_llm(settings), media=media, agent_name=settings.agent_name,
                       max_iterations=settings.llm_max_tool_iterations)
