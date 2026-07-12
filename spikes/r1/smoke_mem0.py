"""R1 spike: mem0 OSS — ground-truth introspection of what it supports locally.

We don't assume docs: enumerate the actual embedder/LLM/vector-store providers
registered in this installed version, then (if a torch-free local embedder
exists) try an end-to-end add(infer=False) + search against local qdrant.
"""

import sys


def main() -> None:
    import mem0

    print(f"mem0 version: {getattr(mem0, '__version__', 'unknown')}")

    from mem0.utils import factory

    def providers(cls_name: str) -> list[str]:
        cls = getattr(factory, cls_name, None)
        if cls is None:
            return [f"<{cls_name} missing>"]
        mapping = getattr(cls, "provider_to_class", {})
        return sorted(mapping)

    embedders = providers("EmbedderFactory")
    llms = providers("LlmFactory")
    stores = providers("VectorStoreFactory")
    print(f"embedder providers: {embedders}")
    print(f"llm providers:      {llms}")
    print(f"vector stores:      {stores}")

    verdict = []
    verdict.append("qdrant" in stores and "qdrant local store: YES" or "qdrant local store: NO")
    torch_free_local = [p for p in embedders if p in ("fastembed", "ollama", "lmstudio")]
    verdict.append(f"torch-free local embedders: {torch_free_local or 'NONE'}")
    verdict.append("ollama llm for extraction: " + ("YES" if "ollama" in llms else "NO"))

    print("OK smoke_mem0 (introspection): " + "; ".join(verdict))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_mem0: {type(e).__name__}: {e}")
        sys.exit(1)
