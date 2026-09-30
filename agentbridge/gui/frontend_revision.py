"""Frontend content identity, computed once when a GUI process starts."""
from hashlib import sha256
from itertools import islice
from pathlib import Path


def frontend_revision(directory: Path) -> str:
    # A partial/custom install must not stop the GUI from starting. The client
    # falls back to the release version if these bounded inputs are unavailable.
    try:
        entries = list(islice(directory.rglob('*'), 513))
        if len(entries) > 512:
            return ''
        paths = sorted(p for p in entries
                       if p.is_file() and p.suffix in ('.js', '.css', '.html'))
        if not paths:
            return ''
        digest = sha256()
        remaining = 16 * 1024 * 1024
        for path in paths:
            name = path.relative_to(directory).as_posix().encode()
            with path.open('rb') as stream:
                content = stream.read(min(remaining, 4 * 1024 * 1024) + 1)
            if len(content) > min(remaining, 4 * 1024 * 1024):
                return ''
            remaining -= len(content)
            digest.update(len(name).to_bytes(8, 'big') + name)
            digest.update(len(content).to_bytes(8, 'big') + content)
        return digest.hexdigest()
    except OSError:
        return ''
