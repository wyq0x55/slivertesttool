# Offline wheels for the collaboration server (run_collab.py)

These wheels let you install the real-time collaboration dependencies WITHOUT
network access. Only the machine that runs `python run_collab.py` needs them;
the Flask web / worker processes do not.

## Target runtime: CPython 3.10

The app runs on **Python 3.10**. All the *pure-python* deps below are shipped as
`py3-none-any` wheels and install on 3.10 offline with no trouble.

`pycrdt`, however, is a compiled Rust extension, so its wheel is specific to a
Python version **and** OS. The two binaries bundled here are **cp311-win** and
**cp312-linux** — neither matches Python 3.10. So on 3.10 you have two options:

### Option A — online / hybrid (recommended, simplest)

Let pip fetch the correct pycrdt binary from PyPI (cp310 wheels exist for
Windows / Linux / macOS). The installer does this automatically — it prefers the
vendored wheels and only reaches out for pycrdt:

```bat
python scripts\install_collab_deps.py
```

or plainly:

```sh
pip install -r requirements-collab.txt
```

### Option B — fully offline on 3.10

Download the matching pycrdt wheel once on a machine with the **same OS + Python
3.10**, drop it next to the others, then install offline:

```sh
pip download pycrdt==0.14.1              # produces pycrdt-0.14.1-cp310-<os>.whl
# copy that .whl into vendor/collab-wheels/
python scripts/install_collab_deps.py    # auto-detects the match -> --no-index
```

## Bundled pycrdt binaries (match automatically if you use these Pythons)

* **Windows x64 + CPython 3.11** — `pycrdt-0.14.1-cp311-cp311-win_amd64.whl`
* **Linux x86_64 + CPython 3.12** — `pycrdt-0.14.1-cp312-cp312-manylinux…whl`

`install_collab_deps.py` detects your interpreter: it does a `--no-index`
offline install when a matching pycrdt wheel is present, otherwise a HYBRID
install (vendored wheels + PyPI for pycrdt only).

## Wheels

| package | version | kind |
|---|---|---|
| pycrdt | 0.14.1 | cp311 win_amd64 (binary) |
| pycrdt | 0.14.1 | cp312 manylinux x86_64 (binary) |
| pycrdt-websocket | 0.16.4 | pure python |
| pycrdt-store | 0.1.5 | pure python |
| sqlite-anyio | 0.2.10 | pure python |
| anyio | 4.14.2 | pure python |
| exceptiongroup | 1.3.1 | pure python |
| idna | 3.18 | pure python |
| typing_extensions | 4.16.0 | pure python |

> For a fully-offline Python 3.10 deployment, add `pycrdt-0.14.1-cp310-<os>.whl`
> to this folder (see Option B above).

After installing, start the server:

```bat
python run_collab.py            :: uvicorn on 0.0.0.0:1234
```
