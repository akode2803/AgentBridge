"""R1 spike: the local-embedding PROBE CHAIN the harness will actually use.

Nothing hardcoded (mission rule): try fastembed (best quality, needs
onnxruntime, blocked by policy on some corporate Windows boxes) and fall back
to model2vec (pure numpy, always loads). The smoke passes if ANY local backend
works, and reports which — mirroring the runtime design for R20.
"""

import sys

import numpy as np

TEXTS = [
    "the deployment pipeline failed on the staging server",
    "our CI build broke during the deploy step",
    "grandma's apple pie recipe needs more cinnamon",
]


def try_fastembed():
    from fastembed import TextEmbedding  # import may raise on blocked onnxruntime

    emb = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return "fastembed/bge-small-en-v1.5", np.array(list(emb.embed(TEXTS)))


def try_model2vec():
    from model2vec import StaticModel

    m = StaticModel.from_pretrained("minishlab/potion-base-8M")
    return "model2vec/potion-base-8M", np.asarray(m.encode(TEXTS))


def main() -> None:
    errors = []
    for probe in (try_fastembed, try_model2vec):
        try:
            backend, vecs = probe()
            break
        except Exception as e:  # noqa: BLE001 — a blocked backend is expected
            errors.append(f"{probe.__name__}: {type(e).__name__}")
    else:
        raise RuntimeError(f"no local embedding backend available: {errors}")

    def cos(a, b):
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

    related, unrelated = cos(vecs[0], vecs[1]), cos(vecs[0], vecs[2])
    assert related > unrelated, (related, unrelated)

    skipped = f" (skipped: {', '.join(errors)})" if errors else ""
    print(f"OK smoke_embeddings: backend={backend}, dim={vecs.shape[1]}, "
          f"related={related:.3f} > unrelated={unrelated:.3f}{skipped}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_embeddings: {type(e).__name__}: {e}")
        sys.exit(1)
