# FML search configurations

Use `run_search` for fresh candidate trajectories. It accepts a source snapshot, a measured baseline score, an English task description, a fixed candidate budget, a new output path and an evaluator. The evaluator receives an isolated directory containing the edited source files. It returns a finite raw metric or raises `CandidateFailure(code, message)` for a candidate-side failure. Unexpected evaluator errors stop the run and preserve the failing slot's record. Multibranch runs require thread-safe model and evaluator callbacks.

| Method | Search choices | Candidate generation |
| --- | --- | --- |
| `ar` | Continue from the best candidate. Keep strict improvements in the configured metric direction. | Propose one change, then edit. |
| `multi_ar` | Each branch maintains its own incumbent. The `branches` argument sets the number. | Propose in branch order. First proposals see prior sibling proposals. Edit and evaluate a wave concurrently. The next wave sees the completed cross-branch journal. A partial final wave respects the remaining budget. |
| `aira_mcts` | Use FML's UCT selection and draft/improve operators. Expand up to two children per selected leaf and propagate successful evaluations along the path. | The draft or improve instruction goes directly to the editor. Failed candidates remain in the journal. Future expansions use successful nodes. |
| `openevolve` | Sample parents and inspirations from the FML island database. Preselect parents for a generation and migrate at the database's configured interval. | Apply the evolution instruction to a parent. Successful children enter the population. Failed slots remain in the trace. |

Search prompt bodies and search primitives come from the archived FML-bench checkout at `d336651ebea50c622c256f02ded82b68b4451fdc`. See `upstream.py` and `LICENSE-FML`. The runner adapts them to shared slot accounting and editor syntax. The standalone multibranch prompts start from a supplied candidate and omit geometric switching signals. The inherited ML-specific wording should be reviewed before comparing performance on non-ML tasks.

## Calling the runner

```python
from pathlib import Path
from autoresearch_rebuild.fml_search import run_search

# initial_sources maps exact relative file names to source text.
# baseline_metric is the measured score of initial_sources on the same evaluator.
records = run_search(
    "multi_ar", sources=initial_sources, baseline=baseline_metric,
    task=task_description, budget=20, branches=3, max_repairs=2,
    output=Path("runs/trial-001"), evaluate=evaluate_candidate,
    metric="score", direction="higher",
)
```

The default model callback uses DeepSeek Flash with thinking disabled and reads its credential from the environment. A custom callback takes a system role and a user prompt and returns text. The transport retries transient upstream errors separately from candidate repairs.

## Editing, accounting and records

Each slot has an initial attempt and up to `max_repairs` additional attempts. An empty proposal or an exhausted proposal API request ends the slot as `proposal_failed`. Keep the error detail when separating provider failures from model behavior. An exhausted editor request or evaluator infrastructure fault records `infrastructure_error` and interrupts the run. A completed slot costs one candidate regardless of its attempt count. Count actual evaluations separately using the attempts with `evaluation_started`. Supplied baseline evaluation is outside the candidate budget.

The editor accepts whole-file blocks and SEARCH/REPLACE blocks. It tries an exact match first and then a unique match under a uniform indentation shift. It adjusts replacement indentation, reports nearby source on a miss, and validates the entire response before publishing the attempt directory. Repairs receive the task, current source and the preceding structured failure. The original proposal remains in the slot record and is omitted from the repair instruction.

Each slot has a `result.json`, editor request/response records and any source snapshots submitted to evaluation. A completed run also writes `trajectory.json`. Cross-slot debug is disabled. Compare candidate count, actual evaluator calls and repair count separately.

## Open review items

- The first no-effect check rejects unchanged source snapshots. An optional `effective(parent_sources, staged_sources)` callback can apply task-specific checks. The semantic effectiveness protocol is pending.
- Omitting original intent during repair allows a candidate to drift. Use the stored proposal, edits and scores when inspecting this behavior.
- Multibranch search starts all branches at the supplied baseline. Proposals are collected serially in each wave; only the first wave passes sibling proposals within the wave. Subsequent waves use the shared journal.
- The source port uses the native default AIRA and OpenEvolve settings. Public reconstruction rules for segment boundaries are documented below. Source-to-target refinements and controller observation schemas can be added separately.

## Continuing from public history

Every algorithm uses the same entry point. Each segment receives the same model and evaluation callbacks as the preceding segment. The caller binds the evaluator to the recorded task and evaluation protocol. Python callables and model credentials are supplied at runtime.

```python
from autoresearch_rebuild.fml_search import resume_search

records = resume_search(
    "runs/ar-001/checkpoint.json", method="aira_mcts", history_mode="inherit",
    budget=5, output="runs/aira-002", model=worker, evaluate=evaluate_candidate,
)
```

The output directory must be new. The segment atomically publishes its own `checkpoint.json` after each terminal slot in global step order. Ancestor source paths are rebased to their existing artifacts; new source snapshots live in the segment directory. Keep the directory tree together when moving an experiment. A completed segment also writes its own `trajectory.json`. An interrupted wave can be restarted from the last published prefix in another directory; already executed but unpublished sibling evaluations can incur additional cost.

**Input fields.** The checkpoint contains `task`, `metric`, `direction` (higher/lower), `candidates`, inclusive `cutoff`, `task_description`, `evaluation_protocol`, `source` and the English `task_system` string. Every candidate carries `id`, `parent_id`, `step`, `code_path`, raw `score`, `description`, `error`, `action` (defaults to improve), and `provenance`. The original baseline occupies step zero with a null parent. Failed candidates carry a null score and can have a null code path. Use an ordinary source file, a source-only directory, or a `*.sources.json` mapping exact target names to text. Relative paths resolve against the checkpoint directory. Provenance records the originating algorithm, native action, terminal status and applicable branch or island.

**Visibility.** Both modes select the best finite raw score through the cutoff, breaking ties by earliest step. Both preserve the visible prefix in the public archive. In `best_only`, only the winning source is loaded and becomes the target algorithm's baseline. Earlier candidate descriptions, journals and source algorithm state are hidden from the model. Its system context includes the original baseline metric as a reference. In `inherit`, the target also reconstructs its native working state from the visible candidates and builds its usual prompts. Original baseline, starting incumbent, and current parent can be distinct.

**AR source, full-history continuation.** The following rules apply to checkpoints produced by AR.

| Target | AR-source initialization and first decision |
| --- | --- |
| AR | Restore the accepted code, baseline reference and original experiment log, including native failure labels. Use the same system and proposal prompt as uninterrupted AR. New checkpoints retain the active log boundary even if AR itself started from a best-only segment. Older checkpoints lacking that boundary default to the native full AR prefix. |
| Multibranch AR | Seed distinct branches from strict running record breakers in reverse chronological order, filling remaining branches with the original baseline. Each branch starts with an empty private log and sees the same AR background. First proposals are sequential and see earlier sibling proposals. The shared journal subsequently records branch activity. |
| AIRA-MCTS | Retain all primary-parent edges including failed nodes. Each scored experiment contributes one observation at its node and each ancestor. UCT selects the first expansion parent; failed terminal nodes cannot become parents. A failed intermediate node can be traversed to reach an evaluated descendant. AR selection labels stay in checkpoint provenance. |
| OpenEvolve | Place the baseline on island zero. At a fork keep the largest scored subtree on the parent's island (chronological tie-break). Assign a sibling subtree to a free island when available; subsequent nodes inherit their lineage's island. Failed attempts remain in the public archive. Only scored programs populate the selection archive and diversity references. Islands can stay empty until native migration. |

**Other native sources, full-history continuation.** Each row specifies the target's first decision and the information available to it.

| Source | AR target | Multibranch AR target | AIRA target | OpenEvolve target |
| --- | --- | --- | --- | --- |
| Multibranch AR | Merge all branches by global step. Recompute record breakers against one incumbent and use the best code with the linear AR prompt. | Restore original K, branch seeds, accepted code, private logs, shared background and journal. A partially completed wave retains its cursor, earlier sibling proposals and pre-wave journal visibility. | Construct one tree by stable identity. Shared ancestors occur once. Backpropagate each scored result once along its primary-parent path, then select by UCT. | Rank branches by their best score including their seed. Give the top M branches one island each; M is the target's `islands` argument (default 3). Ties favor the lower branch ID. See `branch_import.json` for all branch memberships, including failed attempts. Other branches supply labeled reference summaries outside the active population, archive and parent sampler. |
| AIRA | Flatten the tree in global order and start from the global best. | Take the best K strict running records and fill unused positions with the original baseline. Use the shared AIRA prefix and separate branch logs. | Rebuild the original journal, tree, analyses, visits and raw-score totals. Resume an unfinished two-child expansion at the saved parent. | Partition the effective scored tree at actual forks. Prefer shallower forks and whole subtrees; among cuts at the same depth, prefer the largest reduction in the sum of squared island sizes. Split nested forks if more islands are available. A chain remains on one island. |
| OpenEvolve | Use evaluated candidates in global order, recompute strict improvements and continue from the best. | Seed from the best K strict running records among evaluated candidates; use the baseline for remaining branches. | Link real evaluations by their original primary parents. Migrant program instances refer to the source candidate step. The same evaluation contributes one score, regardless of the number of migration copies. | Restore the native database, migration state, RNG and frozen generation plan from the cutoff-specific sidecar. Retain source-branch reference exclusions when OE originally imported a multibranch run. |

**Native state files.** The same output bundle works across machines. Include the referenced ancestor code snapshots and the `oe-state/` directories. OE stores compressed JSON with program membership, primary source steps, archive, feature maps, diversity references, RNG and queued parent/inspiration IDs. Candidate source code is loaded by public step. An older OE checkpoint without its native state file can switch to another algorithm; exact OE continuation reports the missing sidecar. Older multibranch checkpoints without initialization metadata can recover observed branch histories from provenance, but may require an explicit K and cannot guarantee the original prompt background. Older AIRA files without a saved expansion cursor start a fresh expansion.

**OE import conventions.** Local public checkpoints contain one row per candidate slot. Slot provenance records whether evaluation started. Transfers from OE omit known pre-evaluation failures from model-visible experiment history while retaining the slots in the audit trail. For external files that explicitly store migration rows, use `native_action=migrate` or `migration`; `original_candidate_id` or the copy's parent must identify a preceding visible candidate or alias. A scored run with the same code as an earlier run remains a separate observation.

**Unrecognized external sources.** Without source-specific metadata, the public reconstruction defaults are as follows.

| Target | Initial reconstruction in inherit mode |
| --- | --- |
| AR | Replay scores chronologically to rebuild keep/discard/crash history. Start at the historical incumbent. |
| Multibranch AR | Restore matching branch provenance where available. Distribute unassigned or foreign records by `(step - 1) % branches`, reconstruct each branch's incumbent and history, seed empty branches from the global best, and rebuild the shared journal. |
| AIRA-MCTS | Build the parent tree from stable IDs, journal failed nodes, and backpropagate one raw-score observation per successful historical candidate up its ancestry. When an imported child follows a failed parent, attach the searchable node to the nearest usable ancestor; preserve the original parent in the public checkpoint. |
| OpenEvolve | Seed each island with the original baseline. Insert successful candidates chronologically with ancestry, descriptions, complexity and diversity through the native database API, rebuilding the islands, archive, feature map and diversity references. Prefer matching island provenance, otherwise assign by step. Start a fresh migration interval at the boundary. |

External public-history reconstruction can produce different choices from a native continuation.

**Identity.** A segment starting at cutoff 20 emits global steps 21, 22, and so on, including failed slots. Existing integer or string IDs are preserved; new candidates normally use their global step as ID. If an imported ID already occupies that number, the writer uses a collision-free string ID and retains the numeric step for ordering. Result records carry both `parent` (parent step, for the earlier result format) and `parent_id` (the stable public ID). Independent experimental forks have separate lineages; when comparing sibling outputs, qualify a candidate ID by its run directory.

## Fixed-interval controller and sampling

`sample_search` accepts the standard evaluator and worker callbacks together with the editable scope, baseline parameters, global candidate budget B, number of stages T, multibranch width K and repeat count R. Defaults are B=100, T=4, K=5 and R=1. Decision positions are `floor(B*n/T)` for n from zero through T-1. The terminal boundary is B. Each stage spends the difference between its next two boundaries across all branches. Each path has its own candidate IDs; qualify IDs with repeat and path when joining different leaves.

```python
from autoresearch_rebuild.fml_search import sample_search

report = sample_search(
    "independent", sources=initial_sources, baseline=baseline_metric,
    task=task_description, allowed_scope=["solve.py"], baseline_parameters=parameters,
    metric="official_score", direction="higher", evaluate=evaluate_candidate,
    B=10, T=2, K=3, R=1, output="runs/controller-001",
)
```

The controller's JSON action has exactly two string fields, `reason` and `config_id`. The latter is `ar` or `multi_ar`. Its observation reports the original baseline, task, editable scope, budget, current incumbents, number of consecutive stages and slots on the current algorithm, and the prefix grouped by stage and branch. Stage, branch and round indices are zero-based. A partially completed native multibranch wave counts as round zero in the new stage. Parent and candidate IDs retain global path order.

Each syntactically complete edit gets an independent review call with its proposal, the parent sources and the edited sources. The reviewer writes an actual change summary and returns `ok`, `no_op` or `implementation_error`. Review rejection, unapplied edits and evaluator failures share `max_repairs` (default two additional edits). Exact unchanged sources cannot pass the deterministic check. Candidate outcomes in the controller's history are `valid`, `failed` and `no-op`. An exhausted provider/schema error interrupts the run as an infrastructure problem. DeepSeek uses JSON object mode and disabled thinking for the controller and reviewer. The worker uses the existing text interface.

Independent sampling executes R separate paths, each with T controller decisions. Tree sampling enumerates both configurations at each decision without consulting the controller. It executes each prefix once per tree and stores child runs in separate directories. At B=100 and T=4 this means 16 leaves and 30 edges of 25 slots, or 750 physical candidate slots per tree. Each leaf contains 100 logical slots. `observation.json` and each child's `decision.json` preserve the information and action available at the decision. The global `sampling.json` indexes the leaves; each leaf's `path.json` contains all realized stages including the last. Judge attempts and review calls are available in per-slot logs.

## AHC026 joint live smoke

Use the same output root for the following three invocations. They are separate so the first four candidates can be inspected before committing the remaining budget. AHC026 uses the English statement, the supplied Python baseline, the official seed-0 score and DeepSeek Flash with thinking disabled. Set `DEEPSEEK_API_KEY` in the launcher environment. The three parts execute sequentially; the per-process limit is four concurrent API calls. The initial solver is scored once per invocation outside the candidate budget.

```powershell
python -m autoresearch_rebuild.fml_search.ahc026_smoke independent runs/ahc026-joint-01
python -m autoresearch_rebuild.fml_search.ahc026_smoke tree runs/ahc026-joint-01
python -m autoresearch_rebuild.fml_search.ahc026_smoke switches runs/ahc026-joint-01
```

When the launcher is network-restricted, run the API relay in a separate process with network access. The relay accepts the key through a masked prompt and uses the DeepSeek endpoint. Pass the same relay directory to each smoke invocation with `--relay`. The launcher and its evaluators can stay in the workbench sandbox. Create an empty file named `stop` in the relay directory to shut it down, or use the time limit.

```powershell
python -m autoresearch_rebuild.fml_search.api_relay runs/ahc026-joint-01/api-relay --seconds 7200
python -m autoresearch_rebuild.fml_search.ahc026_smoke independent runs/ahc026-joint-01 --relay runs/ahc026-joint-01/api-relay
```

| Part | Physical slots | Acceptance checks |
| --- | ---: | --- |
| Independent, B=4 T=2 K=3 R=1 | 4 | Inspect both real JSON decisions, the second observation's two preceding slots, actual change summaries, scores and any repair feedback. Record model, edit/review latency and repair counts to update the time estimate. |
| Tree, B=10 T=2 K=3 R=1 | 30 | Two shared 5-slot prefixes and four 5-slot suffixes. Check 4 leaves, 6 edges, global parent IDs, no sibling evidence leakage and same-config state continuation. |
| Remaining rotation | 25 | Run 5 fresh slots each for AIRA and OE. Run three 5-slot switches (multibranch AR to OE, OE to AIRA, AIRA to AR). The tree already exercised AR to multibranch AR. K=3 and two OE islands exercise the unselected reference branch. |

The three parts total 59 physical slots. Review a successfully evaluated candidate from each algorithm and inspect the first child at each switch. Treat repeated invalid edits, review schema errors or expired judge calls as diagnostic findings even when the stage reaches its slot count. Wall-clock time depends on actual code generation and repairs. Plan approximately one to two hours and re-estimate after the first part.

## Verification

Run all suites with `python -B -m unittest discover -s autoresearch_rebuild/fml_search -t .`.

From the workspace root run `python -B -m unittest -v autoresearch_rebuild.fml_search.test_search`. Tests inject a deterministic model and evaluator. They cover all four methods, branch visibility and concurrent execution, slot accounting, repair inputs, and atomic edit failure. The AHC026 integration smoke used a scripted edit on public seed 0 and the local official scorer. Moving obstructing boxes two stacks ahead changed the score from 7945 to 8280. This exercises transport between the search and evaluation components; it supplies no evidence about model quality.
