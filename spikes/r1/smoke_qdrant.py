"""R1 spike: qdrant-client LOCAL mode (embedded, no server) on Windows.

Validates the D10/R20 assumption: a persistent on-disk qdrant collection with
upsert + vector search, no server process, no Docker.
"""

import sys
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

SCRATCH = Path(__file__).parent / "_scratch" / "qdrant"


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(path=str(SCRATCH))  # embedded local mode

    name = "smoke"
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))

    client.upsert(
        name,
        points=[
            PointStruct(id=1, vector=[1.0, 0.0, 0.0, 0.0], payload={"txt": "alpha"}),
            PointStruct(id=2, vector=[0.0, 1.0, 0.0, 0.0], payload={"txt": "beta"}),
            PointStruct(id=3, vector=[0.9, 0.1, 0.0, 0.0], payload={"txt": "alpha-ish"}),
        ],
    )

    hits = client.query_points(name, query=[1.0, 0.05, 0.0, 0.0], limit=2).points
    assert hits[0].payload["txt"] in ("alpha", "alpha-ish"), hits
    assert len(hits) == 2

    # persistence: reopen and the collection is still there
    client.close()
    client2 = QdrantClient(path=str(SCRATCH))
    assert client2.collection_exists(name)
    assert client2.count(name).count == 3
    client2.close()

    print("OK smoke_qdrant: embedded local mode, upsert/search/persist verified")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL smoke_qdrant: {type(e).__name__}: {e}")
        sys.exit(1)
