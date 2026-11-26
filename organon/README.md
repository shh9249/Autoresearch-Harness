# Organon V1/V2 reproduction

This branch carries the local V1/V2 implementation as a patch against the [Organon author repository](https://github.com/cheikh025/Organon) at commit `fcd1e501ee589d363766136aceb60527a0f06b12`. The bootstrap script fetches that revision and applies the patch to a fresh checkout.

From the harness repository root:

```bash
bash organon/bootstrap.sh
cd organon_upstream
node --test plugins/dsh-evolve-loop/tests/restricted.node.mjs
```

The generated checkout's `REPRODUCTION.md` describes the Linux and Docker setup and the JavaScript hook contract. V1 lets the model write, activate, version and deactivate custom `loop`, `select` and `survive` plugins. V2 also supports single-candidate guidance and a dynamic `guidance` hook. Candidate generation copies one primary parent and includes any explicitly selected donors as reference code.

An active version keeps its JavaScript variables across candidate iterations and search chunks. Hooks receive a persisted, zero-based `state.turn` and a shared JSON `state.memory` for state that must survive a process restart.

A generator `*loop` chooses the order of generation, evaluation and survival by yielding operations through the fixed Worker and judge. A policy can reuse archived parents, reactivate them in the population and supply multiple distinct donor references.
