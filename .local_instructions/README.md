# Local deployment assets (fairlead homelab)

These files are **not upstream**. They build the `mempalace-mcp` container that serves MemPalace to
openclaw over HTTP on port 3001.

- `Dockerfile` — `python:3.12-slim`, `pip install -e .` from this checkout (so **the version is
  whatever this branch is checked out at**, not a pinned pip release), then copies the wrapper in.
- `mcp_http_server.py` — an HTTP↔stdio bridge. It spawns the `mempalace-mcp` console script with
  `--palace`, proxies JSON-RPC over POST `/`, caches `mempalace_status` and
  `mempalace_get_taxonomy`, and pre-warms the search path (ONNX embeddings + the default backend) on
  first use so the first real query isn't slow.

Recovered out of the running image on 2026-09-20 — before that, these existed **only** inside the
image and the build could not be reproduced. See `plans/mempalace-upgrade/` in the fairlead-ops
corpus.

## Build

    docker build -f .local_instructions/Dockerfile -t 10.0.0.50:5077/mempalace-mcp:<version> .

Build on LXC 100 rather than the Windows dev box, as with ocp. Upgrading = check this branch out on
a newer upstream tag and rebuild; the palace lives outside the image.
