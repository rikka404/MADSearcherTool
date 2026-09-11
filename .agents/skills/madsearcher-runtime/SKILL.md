---
name: madsearcher-runtime
description: Diagnose and fix reproducible MADSearcher Windows Python/FFmpeg/SAM2 runtime issues; use when dependency installation, NDJSON transport, or video memory usage fails in this project.
---

# MADSearcher runtime recovery

Read `Docs/设计文档.md` for the worker protocol before editing transport or media calls.

- **Observed 2026-09-08: dead local proxy.** Git/HTTPS requests attempted `127.0.0.1:7890` and failed because that proxy was not listening. Do not rewrite global Git or network configuration. This project provides `scripts/setup.ps1 -Direct` and `scripts/download_models.py --direct` for one-process direct HTTPS. An empty Git `-c http.proxy=` alone did not resolve inherited environment proxies. Distinguish this connection failure from access/auth errors; do not use direct mode to evade an actual access restriction.
- **SAM2 memory:** the upstream loader preallocates float32 `[N,3,1024,1024]` frames. On the 16GB/6GB GPU development machine, loading a full minute can exhaust RAM. Use the implementation's overlapping 32-frame blocks and CPU offload; never replace it with whole-video `init_state` just to simplify the code. Changing block size affects memory and temporal quality; verify both.
- **Native Windows SAM2:** install with `SAM2_BUILD_CUDA=0` and use `apply_postprocessing=False`; no compiler/CUDA toolkit is required for the optional connected-components extension. CUDA PyTorch wheels still support GPU inference.
- **Observed pip 23.0.1 / PyTorch index:** installation of torch downloaded its wheel, then rejected `typing_extensions` metadata because of hyphen/underscore normalization and attempted unavailable `flit_core` source builds. Upgrade pip inside `.venv` before retrying. New pip uses a different HTTP-cache format, so it may redownload the large wheel. This machine's old cache can be recovered with `scripts/recover_cached_torch.py`, which streams only the wheel bytes and checks the official PyTorch SHA-256 before making an installable file. Install that local wheel using the normal package index for its small dependencies. Do not install unrelated build tools globally.
- **Protocol:** configure UTF-8 for C# redirected streams and Python. Redirect third-party prints to stderr, read stdout/stderr concurrently, and drain the result before disposing the process. Use literal argument arrays for all paths, including Chinese names and spaces. API keys travel through stdin only.
- **Observed skill validation encoding:** the bundled `quick_validate.py` uses the Windows default GBK codec for `read_text()`, which fails on this UTF-8 skill. Invoke it with `python -X utf8 .../quick_validate.py .agents/skills/madsearcher-runtime`; do not re-encode the project's UTF-8 documents as GBK.

Re-run the relevant integration tests and one real command after a recovery. Do not delete original footage or the user's Adobe project to fix caches or dependencies.
