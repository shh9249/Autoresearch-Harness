# Organon with scoped search providers

This branch starts from [author commit fcd1e50](https://github.com/cheikh025/Organon/tree/fcd1e501ee589d363766136aceb60527a0f06b12). The patch applies directly to that commit. Organon's DSH dynamic runner defines, activates, versions and disposes the plugins. Its own provider stack invokes ordinary async JavaScript functions, including their closure state across candidates and successive calls to `evolve_run`.

## Run

On the author's supported Linux/Docker stack, with Git, Node and npm available:

```sh
bash organon/bootstrap.sh
cd organon_upstream
bash setup.sh
```

Start the DSH web process using the command printed by setup, with `ORGANON_VARIANT=v1` (default) or `ORGANON_VARIANT=v2` in **that process's environment**. Use Evolve Mode, the task directory as cwd, and the patched `PROMPT.md`. Use a dedicated DSH process/profile and a separate task workspace per independent experiment. Upstream provider stacks are process-local. In `evolve_status`, the additional `guidance` stack identifies V2. The unchanged author checkout is the unrestricted control.

## Search interface

| Provider | Signature and authority |
| --- | --- |
| V1/V2 `loop` | Native `async run(args, evolve)`. The submitted loop schedules generation, evaluation, recording and survival, and decides its stopping conditions within the pinned candidate budget. It can batch workers with `Promise.all`, compare later, or revisit archived parents. |
| V1/V2 `select` | Native `async select(population, evolve)`. Return a primary candidate ID, or `{parent, donors}` for crossover. The fixed material builder renders the nominated donor sources in order. |
| V1/V2 `survive` | Native `async survive(population, evolve)`. Read, annotate or rewrite rows, including reactivation. Official fitness is checked against each candidate's public evaluation. |
| V2 `guidance` | Register a function taking `({parent, donors}, evolve)` that returns direction text. It can inspect rows, evaluation files and controller state. Alternatively call `evolve.mutate({parent, donors, guidance: text})` from a loop to supply the direction for that candidate. The host appends the text after the fixed worker instruction and donor materials. |

Use normal upstream registration, for example `ctx.effect(() => ctx.evolve.register('loop', {name: 'my-loop', async run(args, evolve) { /* search code */ }}))`. `evolve.files` supports persistent strategy files under the run directory. Candidate directories, official evaluation files, the budget and the population file have dedicated operations. For split or parallel dispatch use `await evolve.allocate(parent)` and then `await evolve.spawnWorker(child.dir)`, optionally passing `{donors, guidance}` (guidance in V2). `evolve.mutate({parent})` uses the original allocation and worker path. Replacing a plugin starts a new closure as in upstream. Use the file store to transfer state deliberately between versions.

## What the patch changes

* **Registration and modifier tools.** The submitted plugin sees the search service and Cordis effect lifecycle. The modifier retains read/search tools and the native search runtime tools. The candidate builder retains the author's confined child tools.
* **Provider calls.** A search-facing service delegates to the original operations. It resolves evaluated candidates by run-owned ID, checks official scores on population writes, and fixes worker dispatch arguments. This boundary is needed even with a registration allowlist because an upstream custom loop can directly invoke the worker with a new persona, prompt, model or tool set.
* **Worker inputs.** A single-parent call with no direction uses the original prompt. The optional donor formatter and V2 direction suffix have a fixed position and format. Natural-language direction is an experimental instruction channel; its semantic content can be audited in the worker prompt.
* **Packaging.** The installer consumes the author's bundled tarballs. Bootstrap rebuilds only the changed loop plugin. Candidate-builder, scoring subprocess, allocation, budgets, selection/survival defaults, benchmarks and DSH itself come from the pinned author version.

The experiment uses the upstream **cooperative** DSH plugin threat model. Its Node VM is not an adversarial JavaScript sandbox. Use an isolated profile and task workspace, as with the original Organon experiments.

## Verification

From the patched checkout on Node 22.19+ (22.x) or 24+:

```sh
cd plugins/dsh-evolve-loop
pnpm install --frozen-lockfile --ignore-scripts
pnpm exec vitest run
```

The author tests exercise the native lifecycle and default search. The added tests use the actual DSH dynamic plugin runner, population, allocation and Python fixture evaluator, with a deterministic stand-in worker. They exercise multiple chunks, parallel generation, separately scheduled evaluations, archived parents, reactivation, five donors, V2 directions and refusal of protected operations. A benchmark run with an actual model is a separate validation.

An additional paired check ran the same unmodified custom plugin on the pinned author source and on V1/V2 with guidance unused. The plugin chose the most recent recorded parent and delegated to the prior loop; the deterministic worker added two to the fixture's integer. With six candidates in three chunks, all three executions had identical worker requests (prompt, persona, model options and shell setting), population records, and chunk results (14, 18, 22). The plugin's persistent turn counter read 1, 2, 3.
