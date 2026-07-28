"""A minimal real ATIF-emitting harbor agent, for the trajectory-contract e2e.

This is the honest mechanism, not a shortcut: it does exactly what
claude-code/goose/terminus do — declare ``SUPPORTS_ATIF`` and write
``trajectory.json`` into ``self.logs_dir`` from ``populate_context_post_run``,
which lands at ``<trial_dir>/agent/trajectory.json`` (the location Harbor's
own viewer reads).  The document it writes is one of Harbor upstream's own
golden ATIF fixtures, so the bytes are what Harbor actually emits.

Lives in its own module (NOT the test file) because importing it requires
harbor; the factory imports it lazily via ``tests.miniatif_agent:MiniAtifAgent``
only inside the harbor-gated integration test.
"""

from __future__ import annotations

from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

GOLDEN_ATIF = (
    Path(__file__).parent
    / "fixtures"
    / "atif"
    / "hello-world-context-summarization.trajectory.json"
)


class MiniAtifAgent(BaseAgent):
    SUPPORTS_ATIF = True

    @staticmethod
    def name() -> str:
        return "mini-atif"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # "Solve" the echo-reward task so the verifier grades it 0.85.
        await environment.exec('sh -c \'echo "probe-rl" > /app/answer.txt\'')

    def populate_context_post_run(self, context: AgentContext) -> None:
        (self.logs_dir / "trajectory.json").write_text(GOLDEN_ATIF.read_text())
