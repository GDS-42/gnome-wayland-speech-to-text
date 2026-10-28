# ADR-0001: sherpa-onnx runtime, Parakeet TDT 0.6B v3 int8 by default

**Status:** Accepted · **Date:** 2026-10-28

## Context
The user wants Parakeet "or another model", French-compatible, on Fedora, with
the ability to update models. There is no desktop software that integrates
these models properly on GNOME/Wayland. Constraints: runs on ordinary laptops
(CPU, no NVIDIA GPU assumed), private (offline), installable without a build
toolchain, several model families so users can trade speed for accuracy.

## Decision
Use **sherpa-onnx** (Apache-2.0, pip wheels for x86_64/aarch64, CPython
3.11–3.15) as the only inference runtime, behind a small `Engine` protocol with
one adapter per model *family*. Ship **NVIDIA Parakeet TDT 0.6B v3 (int8)** as
the recommended default.

Benchmark run while deciding (4 vCPU, no GPU, sherpa-onnx 2.0.1, French clips
from the model archives; "contended" = another job was running):

| Model | Download | RAM | Load | RTF | French output |
|---|---|---|---|---|---|
| Parakeet TDT 0.6B v3 int8 | 487 MB | 0.9 GB | 2.7 s | **0.09–0.11** | perfect, punctuation + hyphens |
| Canary 180M Flash int8 | 154 MB | 0.45 GB | 1.7 s | ~0.3 (contended) | very good, lost a hyphen |
| Cohere Transcribe 14-lang int8 | 1.7 GB | 3.4 GB | 15 s | 0.17–0.33 | perfect |
| Qwen3-ASR 0.6B int8 | 879 MB | 1.6 GB | 5.7 s | ~0.7 (contended) | perfect |
| Whisper large-v3 turbo int8 | 564 MB | 1.6 GB | 6.8 s | ~0.8 (contended) | very good |

## Options considered

### A. sherpa-onnx (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: one pip wheel, ONNX Runtime bundled, same API for every family |
| Cost | Free, Apache-2.0 |
| Coverage | Parakeet, Canary, Whisper, Cohere, Qwen3, SenseVoice, Moonshine… + Silero VAD |
| Maintenance | Very active upstream; models re-exported and hosted by upstream |

Pros: CPU-efficient int8 models, VAD included, model archives hosted on GitHub
releases (reachable, stable URLs). Cons: CUDA requires a different wheel; API
changes between major versions (pinned `>=2.0,<3`).

### B. NVIDIA NeMo (PyTorch)
Reference implementation but multi-GB dependencies, GPU-oriented, slow start.
Rejected for a desktop tool.

### C. faster-whisper / whisper.cpp
Excellent for Whisper only; Parakeet v3 is both more accurate in French and
~8× faster on CPU in our test. Rejected as the core; Whisper remains available
through sherpa-onnx.

### D. onnx-asr
Lightweight and good for Parakeet, but fewer families and no bundled VAD.

## Trade-off analysis
Parakeet v3 is the best speed/accuracy point for French on CPU and detects 25
European languages automatically. Canary covers low-RAM machines, Cohere the
"maximum accuracy" niche, Qwen3/Whisper broad language coverage. One runtime
keeps packaging and bug surface small.

## Consequences
- New families need an adapter in `greffier/stt/families.py` and a catalog entry.
- GPU users must install sherpa-onnx's CUDA build manually (documented).
- Revisit when sherpa-onnx 3.x ships or when a streaming French model matures.
