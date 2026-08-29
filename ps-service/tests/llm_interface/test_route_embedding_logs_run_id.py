"""AC-005 test: a RouteEmbedding call emits a structured log entry with the bound run_id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from litellm.types.utils import Embedding, EmbeddingResponse

from ps_service.llm_interface.embedding import route_embedding
from ps_service.logging import bind_run_context

if TYPE_CHECKING:
    from conftest import MakeEmitter, ReadLines


def test_route_embedding_when_run_id_bound_then_emits_log_entry_with_bound_run_id(
    make_emitter: MakeEmitter, read_lines: ReadLines
) -> None:
    emitter, log_path = make_emitter()
    fake_response = EmbeddingResponse(
        model="fake-embed-model",
        data=[Embedding(embedding=[0.1, 0.2, 0.3], index=0, object="embedding")],
    )

    def fake_call_embedding(*, model: str, inputs: list[str], timeout: float) -> EmbeddingResponse:
        return fake_response

    with bind_run_context("run-llm-005"):
        route_embedding(
            "some text",
            model="fake-embed-model",
            call_embedding=fake_call_embedding,
            emitter=emitter,
        )
    emitter.flush()

    lines = read_lines(log_path)
    assert lines, "no entries were written — wiring bug"
    assert lines[-1]["run_id"] == "run-llm-005"
    assert lines[-1]["action"] == "route_embedding"
