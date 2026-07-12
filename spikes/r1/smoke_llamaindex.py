"""R1 spike: llama-index retrieval WITHOUT any LLM, using OUR OWN embedding
backend (the probe chain from smoke_embeddings) via a custom BaseEmbedding.

Validates two R21 assumptions at once:
  1. VectorStoreIndex + as_retriever().retrieve() needs no LLM.
  2. llama-index accepts a custom embedder, so our probe-chain interface plugs
     in regardless of which local backend a machine can run.
"""

import sys

from llama_index.core import Document, Settings, VectorStoreIndex
from llama_index.core.embeddings import BaseEmbedding


def load_local_backend():
    """Same probe order as smoke_embeddings: fastembed -> model2vec."""
    try:
        from fastembed import TextEmbedding

        emb = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        return "fastembed", lambda texts: [list(v) for v in emb.embed(texts)]
    except Exception:  # noqa: BLE001 — blocked onnxruntime etc.
        from model2vec import StaticModel

        m = StaticModel.from_pretrained("minishlab/potion-base-8M")
        return "model2vec", lambda texts: [list(v) for v in m.encode(texts)]


BACKEND_NAME, EMBED = load_local_backend()


class ProbeChainEmbedding(BaseEmbedding):
    """Thin llama-index adapter over the harness's local embedding backend."""

    def _get_query_embedding(self, query: str) -> list[float]:
        return EMBED([query])[0]

    def _get_text_embedding(self, text: str) -> list[float]:
        return EMBED([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return EMBED(texts)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


def main() -> None:
    Settings.embed_model = ProbeChainEmbedding()
    Settings.llm = None  # retrieval must not need one

    docs = [
        Document(text="The mesh outbox queue retries failed sends with backoff.", id_="d1"),
        Document(text="Group admins can toggle whether members may add others.", id_="d2"),
        Document(text="Fable pie recipe: apples, cinnamon, brown butter crust.", id_="d3"),
    ]
    index = VectorStoreIndex.from_documents(docs)
    hits = index.as_retriever(similarity_top_k=2).retrieve(
        "what happens when a message fails to send?"
    )
    assert hits, "no retrieval results"
    top_text = hits[0].node.get_content()
    assert "outbox" in top_text, f"wrong top hit: {top_text!r}"

    print(f"OK smoke_llamaindex: retriever-without-LLM via custom embedder "
          f"({BACKEND_NAME}), top score={hits[0].score:.3f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_llamaindex: {type(e).__name__}: {e}")
        sys.exit(1)
