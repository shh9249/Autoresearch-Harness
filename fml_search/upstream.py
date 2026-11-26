"""Selected FML-Bench search structures and prompts. See LICENSE-FML and README.md."""

from __future__ import annotations
import difflib, logging, math, random, re, ast
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
logger = logging.getLogger(__name__)

def normalise_q_value(q_value: float, global_max_q: float, global_min_q: float) -> float:
    """Normalise q-value to [0, 1] range."""
    if global_max_q == global_min_q:
        return 0.5
    return (q_value - global_min_q) / (global_max_q - global_min_q)


def uct_value(
    node: "MCTSNode",
    parent: "MCTSNode",
    uct_c: float,
    global_max_q: float,
    global_min_q: float,
    direction: str,
) -> float:
    """Compute UCT value for a child node."""
    if node.explore_count == 0:
        return -1e8
    norm_q = normalise_q_value(node.q_value(direction), global_max_q, global_min_q)
    exploration = math.sqrt(math.log(parent.explore_count) / node.explore_count)
    return norm_q + uct_c * exploration


def search_policy(
    root: "MCTSNode",
    uct_c: float,
    global_max_q: float,
    global_min_q: float,
    direction: str,
) -> List["MCTSNode"]:
    """Traverse from root to leaf using UCT selection.  Returns path list."""
    path: List[MCTSNode] = []
    node = root
    while True:
        path.append(node)
        selectable = [c for c in node.children if not c.is_buggy or c.explore_count > 0]
        if not selectable:
            return path
        node = max(
            selectable,
            key=lambda c: uct_value(c, node, uct_c, global_max_q, global_min_q, direction),
        )


class MCTSNode:
    """A node in the MCTS tree."""

    _id_counter = 0

    def __init__(
        self,
        plan: str = "",
        action: str = "",
        code_snapshot: Optional[Dict[str, str]] = None,
        parent: Optional["MCTSNode"] = None,
        val_result: Optional[Dict[str, Any]] = None,
        primary_metric: Optional[float] = None,
        is_buggy: bool = False,
        error_context: Optional[str] = None,
        step_id: Optional[int] = None,
    ):
        MCTSNode._id_counter += 1
        self.node_id: int = MCTSNode._id_counter
        self.step_id: Optional[int] = step_id
        self.plan: str = plan
        self.action: str = action  # "draft" | "improve" | "debug"
        self.code_snapshot: Optional[Dict[str, str]] = code_snapshot
        self.parent: Optional[MCTSNode] = parent
        self.children: List[MCTSNode] = []
        self.val_result: Optional[Dict[str, Any]] = val_result
        self.primary_metric: Optional[float] = primary_metric
        self.is_buggy: bool = is_buggy
        self.error_context: Optional[str] = error_context
        self.analysis: str = ""

        # MCTS statistics
        self.explore_count: int = 0
        self.cumulative_value: float = 0.0

    # -- MCTS value methods -------------------------------------------------

    def q_value(self, direction: str) -> float:
        """Average value.  *direction* is 'higher' or 'lower'."""
        if self.explore_count == 0:
            return float("-inf")
        q = self.cumulative_value / self.explore_count
        if direction == "lower":
            q = -q
        return q

    def add_value(self, v: float) -> None:
        if v is not None:
            self.cumulative_value += v

    def increment_explore_count(self) -> None:
        self.explore_count += 1

    # -- Tree properties ----------------------------------------------------

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def stage_name(self) -> str:
        return self.action or "root"

    @property
    def debug_depth(self) -> int:
        """Count consecutive debug ancestors (including self)."""
        depth = 0
        node = self
        while node is not None and node.action == "debug":
            depth += 1
            node = node.parent
        return depth

    def __repr__(self) -> str:
        return (
            f"MCTSNode(id={self.node_id}, action={self.action}, "
            f"metric={self.primary_metric}, buggy={self.is_buggy}, "
            f"visits={self.explore_count})"
        )


class Journal:
    """Append-only log of all MCTSNodes."""

    def __init__(self):
        self.nodes: List[MCTSNode] = []

    def append(self, node: MCTSNode) -> None:
        self.nodes.append(node)

    # -- Filtered views -----------------------------------------------------

    @property
    def draft_nodes(self) -> List[MCTSNode]:
        return [n for n in self.nodes if n.action == "draft"]

    @property
    def buggy_nodes(self) -> List[MCTSNode]:
        return [n for n in self.nodes if n.is_buggy]

    @property
    def good_nodes(self) -> List[MCTSNode]:
        return [n for n in self.nodes if not n.is_buggy and n.primary_metric is not None]

    def get_best_node(self, direction: str) -> Optional[MCTSNode]:
        """Return the node with the best primary_metric."""
        good = self.good_nodes
        if not good:
            return None
        if direction == "higher":
            return max(good, key=lambda n: n.primary_metric)
        else:
            return min(good, key=lambda n: n.primary_metric)

    # -- Memory context for LLM prompts ------------------------------------

    def build_memory_context(self, direction: str,
                             metric_name: str = "metric") -> str:
        """Summarise good nodes for LLM context (memory operator)."""
        good = self.good_nodes
        if not good:
            return "(No successful attempts yet.)"
        # Sort best first
        good_sorted = sorted(
            good,
            key=lambda n: n.primary_metric,
            reverse=(direction == "higher"),
        )
        lines = []
        for n in good_sorted[:10]:  # top-10 summaries
            lines.append(
                f"- [Node {n.node_id}] Action={n.action}, "
                f"Plan: {n.plan[:200]}, Analysis: {n.analysis[:200]}, "
                f"{metric_name}={n.primary_metric}"
            )
        return "\n".join(lines)


@dataclass
class Program:
    """A single program in the evolutionary population.

    Ported from OpenEvolve database.py Program dataclass.
    code_snapshot is Dict[str, str] (multi-file) instead of single 'code' string.
    """

    id: str = ""
    code_snapshot: Dict[str, str] = field(default_factory=dict)
    parent_id: Optional[str] = None
    generation: int = 0
    island: int = 0
    step_id: Optional[int] = None
    action: str = ""  # "seed" | "evolve" | "debug"
    primary_metric: Optional[float] = None
    is_buggy: bool = True
    error_context: str = ""
    analysis: str = ""
    changes_description: str = ""
    complexity: float = 0.0
    diversity: float = 0.0
    migrated: bool = False  # True if migrated from another island
    debug_depth: int = 0  # consecutive debug ancestors count
    val_result: Optional[Dict[str, Any]] = None  # Full val result for logging
    edit_success: bool = True  # Whether CodeEditor edit succeeded


_DIVERSITY_CODE_MAX_LEN = 23000


class ProgramDatabase:
    """MAP-Elites population with island model.

    Simplified from OpenEvolve's ~3600-line database.py for sequential
    single-threaded execution in FML-bench.
    """

    def __init__(
        self,
        num_islands: int = 3,
        feature_bins: int = 5,
        archive_size: int = 30,
        exploration_ratio: float = 0.2,
        exploitation_ratio: float = 0.7,
        migration_interval: int = 10,
        migration_rate: float = 0.1,
        metric_direction: str = "higher",
        diversity_reference_size: int = 10,
        elite_selection_ratio: float = 0.1,
    ):
        self.rng = random.Random()
        self.num_islands = num_islands
        self.feature_bins = feature_bins
        self.archive_size = archive_size
        self.exploration_ratio = exploration_ratio
        self.exploitation_ratio = exploitation_ratio
        self.migration_interval = migration_interval
        self.migration_rate = migration_rate
        self.metric_direction = metric_direction
        self.diversity_reference_size = diversity_reference_size
        # Inspiration sampling: fraction of n used as top-elite slots in step 2
        # of _sample_inspirations (matches original database.py:1601).
        self.elite_selection_ratio = elite_selection_ratio

        # Program storage
        self.programs: Dict[str, Program] = {}

        # Per-island structures
        self.islands: List[Set[str]] = [set() for _ in range(num_islands)]
        # MAP-Elites grid per island: feature_key -> program_id
        self.island_feature_maps: List[Dict[str, str]] = [
            {} for _ in range(num_islands)
        ]
        self.island_best_programs: List[Optional[str]] = [None] * num_islands
        self.island_generations: List[int] = [0] * num_islands

        # Global structures
        self.archive: Set[str] = set()
        self.best_program_id: Optional[str] = None
        self.last_migration_gen: int = 0

        # Feature scaling (running min/max for binning)
        self._complexity_min: float = float("inf")
        self._complexity_max: float = float("-inf")
        self._diversity_min: float = 0.0
        self._diversity_max: float = 1.0

        # Reference set for diversity calculation (greedy max-diversity selection)
        self._reference_programs: list = []

    # -- Core operations ---------------------------------------------------

    def add(self, program: Program, iteration: int = 0) -> None:
        """Add a program to the database. Handles MAP-Elites placement,
        archive update, and best-program tracking.

        Ported from database.py add() (lines 211-368).
        """
        if not program.id:
            program.id = f"p{len(self.programs):012d}"
        self.programs[program.id] = program
        island_id = program.island

        # Add to island
        self.islands[island_id].add(program.id)

        # Update feature scaling
        if program.complexity < self._complexity_min:
            self._complexity_min = program.complexity
        if program.complexity > self._complexity_max:
            self._complexity_max = program.complexity

        # Calculate feature coordinates and place in MAP-Elites grid
        feature_key = self._calculate_feature_key(program)
        fmap = self.island_feature_maps[island_id]

        if feature_key not in fmap:
            # New cell — place directly
            fmap[feature_key] = program.id
        else:
            # Occupied cell — keep better program
            existing = self.programs.get(fmap[feature_key])
            if existing is None or self._is_better(program, existing):
                fmap[feature_key] = program.id

        # Update archive (top programs globally)
        self._update_archive(program)

        # Update best tracking
        self._update_best(program, island_id)

        # Update diversity reference set (greedy max-diversity selection)
        if not program.is_buggy:
            self._update_diversity_reference_set()

    def _update_diversity_reference_set(self) -> None:
        """Greedy max-diversity reference set selection.

        Ported from original database.py:2108-2146.  Selects a subset of
        programs that maximises pairwise diversity, used as the reference
        for ``compute_diversity()``.
        """
        all_codes = [
            self._concat_code(p.code_snapshot)[:_DIVERSITY_CODE_MAX_LEN]
            for p in self.programs.values()
            if not p.is_buggy and p.code_snapshot
        ]
        if not all_codes:
            return
        if len(all_codes) <= self.diversity_reference_size:
            self._reference_programs = list(all_codes)
            return

        # Greedy: start with random seed, add program maximising min-distance
        remaining = list(all_codes)
        selected = [remaining.pop(self.rng.randint(0, len(remaining) - 1))]
        while len(selected) < self.diversity_reference_size and remaining:
            best_idx, best_min_div = -1, -1.0
            for i, candidate in enumerate(remaining):
                min_div = min(
                    1.0 - difflib.SequenceMatcher(None, candidate, sel).ratio()
                    for sel in selected
                )
                if min_div > best_min_div:
                    best_min_div = min_div
                    best_idx = i
            if best_idx >= 0:
                selected.append(remaining.pop(best_idx))
            else:
                break
        self._reference_programs = selected

    def sample(self, island_id: int) -> Tuple[Program, List[Program]]:
        """Sample a parent + inspiration programs from an island.

        Three-strategy sampling (from database.py lines 1270-1288):
          - Exploration (20%): random from island
          - Exploitation (70%): from archive on this island
          - Weighted (10%): fitness-proportionate from island

        Returns: (parent, inspirations)
        """
        parent = self._sample_parent(island_id)
        # Use parent.island (parent's home) for inspirations — matches
        # original openevolve/database.py:1571 where _sample_inspirations
        # derives parent_island = parent.metadata.get("island", ...).
        # Critical for the cross-island archive fallback path in
        # _sample_parent: a parent borrowed from another island must
        # draw its inspirations from its own home island, preserving
        # island isolation semantics.
        inspirations = self._sample_inspirations(parent, parent.island)
        return parent, inspirations

    def get_top_programs(self, n: int, island_id: int) -> List[Program]:
        """Get top N programs from an island by metric.

        Ported from database.py get_top_programs() (lines 538-588).
        """
        island_programs = [
            self.programs[pid]
            for pid in sorted(self.islands[island_id])
            if pid in self.programs and not self.programs[pid].is_buggy
        ]
        if not island_programs:
            return []

        if self.metric_direction == "higher":
            island_programs.sort(
                key=lambda p: p.primary_metric if p.primary_metric is not None else float("-inf"),
                reverse=True,
            )
        else:
            island_programs.sort(
                key=lambda p: p.primary_metric if p.primary_metric is not None else float("inf"),
            )
        return island_programs[:n]

    def get_best_program(self) -> Optional[Program]:
        """Get the globally best program."""
        if self.best_program_id and self.best_program_id in self.programs:
            return self.programs[self.best_program_id]
        return None

    def migrate_programs(self) -> None:
        """Bidirectional ring-topology migration.

        Ported from database.py migrate_programs() (lines 1780-1877).
        Original uses bidirectional ring: each island donates to both neighbors.
        Migration count is per-island based on island size.
        Programs already migrated are skipped to prevent re-migration.
        """
        for src_island in range(self.num_islands):
            island_size = len(self.islands[src_island])
            n_migrate = max(1, int(island_size * self.migration_rate))

            # Bidirectional: donate to both neighbors
            dst_islands = [
                (src_island + 1) % self.num_islands,
                (src_island - 1) % self.num_islands,
            ]
            # Deduplicate (matters when num_islands == 2)
            dst_islands = list(set(dst_islands))

            top = self.get_top_programs(n_migrate, src_island)

            for prog in top:
                # Skip re-migration of already-migrated programs
                if prog.migrated:
                    continue
                for dst_island in dst_islands:
                    if dst_island == src_island:
                        continue
                    migrant = Program(
                        code_snapshot=dict(prog.code_snapshot),
                        parent_id=prog.id,
                        step_id=prog.step_id,
                        generation=prog.generation,
                        island=dst_island,
                        action="evolve",
                        primary_metric=prog.primary_metric,
                        is_buggy=prog.is_buggy,
                        analysis=prog.analysis,
                        changes_description=(
                            f"[migrated from island {src_island}] "
                            f"{prog.changes_description}"
                        ),
                        complexity=prog.complexity,
                        diversity=prog.diversity,
                        migrated=True,
                    )
                    self.add(migrant, iteration=0)

        self.last_migration_gen = max(self.island_generations)
        logger.info("Migration complete")

    # -- Internal helpers --------------------------------------------------

    def _sample_parent(self, island_id: int) -> Program:
        """Three-strategy sampling from an island.

        Ported from database.py _sample_parent() (lines 1270-1288) and the
        parallel path at sample_from_island() (lines 428-448).
        """
        island_pids = sorted(self.islands[island_id])
        if not island_pids:
            raise ValueError(f"Island {island_id} is empty, cannot sample.")

        r = self.rng.random()

        # Exploration: random from island
        if r < self.exploration_ratio:
            pid = self.rng.choice(island_pids)
            return self.programs[pid]

        # Exploitation: from archive, with multi-level fallback chain.
        # Ported faithfully from _sample_from_archive_for_island()
        # (database.py:1515-1552).
        if r < self.exploration_ratio + self.exploitation_ratio:
            # Clean up stale archive references
            valid_archive = [pid for pid in sorted(self.archive) if pid in self.programs]

            if not valid_archive:
                # (a) Entire archive empty → fall back to weighted island
                return self._sample_from_island_weighted(island_id)

            archive_on_island = [
                pid for pid in valid_archive
                if self.programs[pid].island == island_id
            ]
            if archive_on_island:
                return self.programs[self.rng.choice(archive_on_island)]

            # (b) Island has no archive programs → random from entire archive
            return self.programs[self.rng.choice(valid_archive)]

        # Weighted: delegate to extracted helper (also used by fix #1 fallback).
        return self._sample_from_island_weighted(island_id)

    def _sample_from_island_weighted(self, island_id: int) -> Program:
        """Fitness-proportionate sampling from a specific island.

        Faithfully matches original database.py:1427-1482 for "higher is
        better" metrics. For "lower is better" (a FML-bench extension —
        the original only supports higher), we reflect the metric around
        the scored range midpoint so that:
          - The "lower is better" direction is symmetric to "higher is
            better" for the same metric values (same distribution shape,
            just with best/worst ordering flipped).
          - A single scored program always receives a positive fitness
            equal to its metric, so it dominates any buggy program.

        Weighting rules:
        - Empty island → global random fallback (original calls
          `_sample_random_parent()`; we use `random.choice` over all
          programs — equivalent).
        - Each program gets `max(fitness, EPSILON)` weight, matching the
          original's line 1463 `max(fitness, 0.001)` behavior.
        - Direction-aware raw fitness:
          - "higher is better" (default): fitness = primary_metric
          - "lower is better":           fitness = (max_s + min_s) - metric
            where max_s = max(scored_metrics), min_s = min(scored_metrics).
            This reflects the metric around the midpoint of the scored
            range — equivalent to using "higher is better" weighting on
            the metric values but with the ordering flipped.
          - Buggy / unmeasured programs: fitness = 0 → floored to EPSILON.

        Bug history (captured here so future maintainers understand why
        this is not simpler):
        1. Our first port used shift-based weighting (`metric - min + 1e-6`)
           which gave the worst scored program a weight of 1e-6, *lower*
           than buggy's EPSILON=1e-3, causing `buggy > worst_scored` in the
           sampling distribution. Fixed by switching to raw-fitness.
        2. The naive raw-fitness "lower" transform (`max_scored - metric`)
           has an edge case: when only 1 scored program exists, its fitness
           is `max - max = 0`, which then gets floored to EPSILON — tied
           with buggy. Fixed by using `(max + min) - metric` instead, so
           a single scored program receives fitness = metric > EPSILON.
        """
        island_pids = sorted(self.islands[island_id])
        if not island_pids:
            # Empty island → fall back to any program globally.
            # Matches original _sample_from_island_weighted line 1440-1443
            # which calls _sample_random_parent().
            if not self.programs:
                raise ValueError("Database has no programs")
            return self.rng.choice(list(self.programs.values()))

        island_programs = [
            self.programs[pid] for pid in island_pids if pid in self.programs
        ]
        if not island_programs:
            return self.programs[self.rng.choice(island_pids)]
        if len(island_programs) == 1:
            return island_programs[0]

        EPSILON = 1e-3  # Floor — matches original's max(fitness, 0.001)

        # Collect scored metrics to compute direction-aware fitness.
        scored_metrics = [
            p.primary_metric for p in island_programs
            if not p.is_buggy and p.primary_metric is not None
        ]
        if not scored_metrics:
            # All programs buggy/unmeasured → uniform random
            return self.rng.choice(island_programs)

        # Direction-aware raw fitness per program.
        #   higher: fitness = primary_metric
        #   lower:  fitness = (max_scored + min_scored) - primary_metric
        # Buggy / unmeasured always get fitness = 0 → EPSILON after flooring.
        if self.metric_direction == "lower":
            max_scored = max(scored_metrics)
            min_scored = min(scored_metrics)
            reflection_sum = max_scored + min_scored
            def _fitness(p: Program) -> float:
                if p.is_buggy or p.primary_metric is None:
                    return 0.0
                return reflection_sum - p.primary_metric
        else:
            def _fitness(p: Program) -> float:
                if p.is_buggy or p.primary_metric is None:
                    return 0.0
                return p.primary_metric

        weights = [max(_fitness(p), EPSILON) for p in island_programs]

        # Normalize and sample (matches original lines 1466-1470).
        total = sum(weights)
        if total > 0:
            weights = [w / total for w in weights]
        else:
            weights = [1.0 / len(island_programs)] * len(island_programs)
        return self.rng.choices(island_programs, weights=weights, k=1)[0]

    def _sample_inspirations(
        self, parent: Program, island_id: int, n: int = 3
    ) -> List[Program]:
        """Sample inspiration programs (diverse approaches).

        Ported faithfully from database.py _sample_inspirations()
        (lines 1554-1676). Four-step structure:
          1. Island best (if != parent)
          2. get_top_programs(max(1, int(n * elite_selection_ratio)))
          3. Feature-neighborhood perturbation (±2 bins on both axes)
          4. Random fill from island to reach n

        Steps 3 and 4 are both nested inside the original's line-1608
        guard `len(island_programs) > n and len(inspirations) < n` — when
        the island is saturated (≤ n programs) both steps are skipped.
        """
        inspirations: List[Program] = []
        seen_ids = {parent.id}

        # Step 1: Island best (if != parent)
        best_pid = self.island_best_programs[island_id]
        if best_pid and best_pid != parent.id and best_pid in self.programs:
            inspirations.append(self.programs[best_pid])
            seen_ids.add(best_pid)

        # Step 2: Top elite by ratio (NOT n*2). With n=3 and default
        # elite_selection_ratio=0.1, top_n = max(1, int(0.3)) = 1. This
        # often duplicates the island-best so contributes zero after dedup.
        top_n = max(1, int(n * self.elite_selection_ratio))
        top = self.get_top_programs(top_n, island_id)
        for p in top:
            if p.id not in seen_ids:
                inspirations.append(p)
                seen_ids.add(p.id)

        # Steps 3+4 share the same guard (matches original database.py:1608):
        #   only fire when the island has strictly more than n programs AND
        #   we haven't already filled n slots. If the island has <= n
        #   programs we skip both (matches original behavior — there's
        #   nothing meaningful to perturb or fill against when saturated).
        island_pids = sorted(self.islands[island_id])
        if len(island_pids) > n and len(inspirations) < n:
            remaining_slots = n - len(inspirations)
            parent_coords = self._calculate_feature_coords(parent)
            nearby: List[Program] = []

            # Build the island's feature-map: {feature_key: program_id}.
            # When multiple programs fall in the same cell, the last one
            # wins (same as original — dict assignment overwrites).
            island_feature_map: Dict[str, str] = {}
            for pid in island_pids:
                if pid in self.programs:
                    key = self._calculate_feature_key(self.programs[pid])
                    island_feature_map[key] = pid

            # Step 3: Feature-neighborhood perturbation.
            # With feature_bins=5 and ±2 perturbation on each axis, the
            # sampled cell can be ANY cell on the island (±2 covers the
            # full [0, feature_bins-1] range from any starting point).
            # Locality is coarse — matches original's behavior with
            # default feature_bins and is a known limitation of the
            # 5-bin setup. For larger feature_bins perturbation becomes
            # genuinely local.
            for _ in range(remaining_slots * 3):
                perturbed = (
                    max(0, min(self.feature_bins - 1,
                               parent_coords[0] + self.rng.randint(-2, 2))),
                    max(0, min(self.feature_bins - 1,
                               parent_coords[1] + self.rng.randint(-2, 2))),
                )
                key = f"{perturbed[0]}_{perturbed[1]}"
                cand_pid = island_feature_map.get(key)
                # Exclude parent + step-1/2 picks + already-nearby picks via
                # seen_ids (updated eagerly to allow step 4's single-set
                # exclusion check).
                if cand_pid and cand_pid not in seen_ids:
                    nearby.append(self.programs[cand_pid])
                    seen_ids.add(cand_pid)
                    if len(nearby) >= remaining_slots:
                        break

            # Step 4: Random fill from remaining island programs — still
            # inside the len(island_pids) > n guard, matching original
            # line 1646 where the fill is nested under the same condition.
            if len(inspirations) + len(nearby) < n:
                remaining = n - len(inspirations) - len(nearby)
                available = [
                    pid for pid in island_pids
                    if pid in self.programs and pid not in seen_ids
                ]
                if available:
                    fill_ids = self.rng.sample(
                        available, min(remaining, len(available))
                    )
                    nearby.extend(self.programs[pid] for pid in fill_ids)

            inspirations.extend(nearby)

        return inspirations[:n]

    def _calculate_feature_coords(self, program: Program) -> Tuple[int, int]:
        """Return raw (c_bin, d_bin) integer coordinates for a program.

        Ported from database.py _calculate_feature_coords() (lines 834-900).
        Uses complexity (code length) and diversity (edit distance) as the
        two feature dimensions. Used by both _calculate_feature_key()
        (returns a string form) and _sample_inspirations() feature
        perturbation (needs raw ints).
        """
        # Bin complexity
        c_range = self._complexity_max - self._complexity_min
        if c_range > 0:
            c_norm = (program.complexity - self._complexity_min) / c_range
        else:
            c_norm = 0.5
        c_bin = min(int(c_norm * self.feature_bins), self.feature_bins - 1)

        # Bin diversity
        d_range = self._diversity_max - self._diversity_min
        if d_range > 0:
            d_norm = (program.diversity - self._diversity_min) / d_range
        else:
            d_norm = 0.5
        d_bin = min(int(d_norm * self.feature_bins), self.feature_bins - 1)

        return (c_bin, d_bin)

    def _calculate_feature_key(self, program: Program) -> str:
        """MAP-Elites feature key (string form, used as map key).

        Thin wrapper over _calculate_feature_coords. The string format
        `"c_bin_d_bin"` is used as the key in island_feature_maps.
        """
        c_bin, d_bin = self._calculate_feature_coords(program)
        return f"{c_bin}_{d_bin}"

    def _is_better(self, p1: Program, p2: Program) -> bool:
        """Compare two programs by primary metric."""
        if p1.primary_metric is None:
            return False
        if p2.primary_metric is None:
            return True
        if self.metric_direction == "higher":
            return p1.primary_metric > p2.primary_metric
        return p1.primary_metric < p2.primary_metric

    def _update_archive(self, program: Program) -> None:
        """Update global archive of elite programs.

        Ported from database.py archive management (lines 1131-1176).
        """
        if program.is_buggy:
            return

        self.archive.add(program.id)

        # Trim archive to max size
        if len(self.archive) > self.archive_size:
            archive_programs = [
                self.programs[pid]
                for pid in sorted(self.archive)
                if pid in self.programs
            ]
            if self.metric_direction == "higher":
                archive_programs.sort(
                    key=lambda p: p.primary_metric if p.primary_metric is not None else float("-inf"),
                )
            else:
                archive_programs.sort(
                    key=lambda p: p.primary_metric if p.primary_metric is not None else float("inf"),
                    reverse=True,
                )
            # Remove worst
            while len(self.archive) > self.archive_size and archive_programs:
                worst = archive_programs.pop(0)
                self.archive.discard(worst.id)

    def _update_best(self, program: Program, island_id: int) -> None:
        """Update best program tracking (global + per-island).

        Ported from database.py best tracking (lines 1178-1217).
        """
        if program.is_buggy:
            return

        # Per-island best
        current_island_best = self.island_best_programs[island_id]
        if current_island_best is None or current_island_best not in self.programs:
            self.island_best_programs[island_id] = program.id
        elif self._is_better(program, self.programs[current_island_best]):
            self.island_best_programs[island_id] = program.id

        # Global best
        if self.best_program_id is None or self.best_program_id not in self.programs:
            self.best_program_id = program.id
        elif self._is_better(program, self.programs[self.best_program_id]):
            self.best_program_id = program.id

    @staticmethod
    def _concat_code(code_snapshot: Dict[str, str]) -> str:
        """Concatenate all files in a code snapshot into a single string."""
        return "\n".join(code_snapshot.get(k, "") for k in sorted(code_snapshot))


def compute_complexity(code_snapshot: Dict[str, str]) -> float:
    """Complexity feature: total code length across all target files."""
    return float(sum(len(content) for content in code_snapshot.values()))


def compute_diversity(
    code_snapshot: Dict[str, str], reference_programs: List[str]
) -> float:
    """Diversity feature: average edit distance to reference set.

    Uses difflib.SequenceMatcher for fast approximation. Returns value in [0, 1].
    Ported from OpenEvolve database.py diversity_metric="edit_distance".
    """
    if not reference_programs:
        return 0.5  # neutral default

    code_str = ProgramDatabase._concat_code(code_snapshot)[:_DIVERSITY_CODE_MAX_LEN]
    total_dist = 0.0
    for ref in reference_programs:
        ratio = difflib.SequenceMatcher(None, code_str, ref).ratio()
        total_dist += 1.0 - ratio  # distance = 1 - similarity
    return total_dist / len(reference_programs)


BRANCH_INIT_TEMPLATE = (
    "[BRANCH INITIALISATION]\n"
    "You are starting exploration branch {branch_id} from the supplied starting candidate.\n\n"
    "## Parent candidate\n"
    "- Step {parent_step}, {metric_name} = {parent_metric}\n"
    "- Description: {parent_idea}\n\n"
    "## Other branches' first ideas (siblings; for diversity)\n"
    "{sibling_block}\n\n"
    "## Cross-branch journal (recent activity from all branches)\n"
    "{journal_block}\n\n"
    "## REQUIRED PROPERTY OF YOUR FIRST IDEA\n"
    "Your first idea on this branch MUST be **mechanistically different** from any "
    "sibling first idea listed above. Specifically:\n"
    "  - A different algorithmic family, OR\n"
    "  - A different mathematical formulation, OR\n"
    "  - A different conceptual approach.\n"
    "Hyperparameter tweaks or surface-level edits of a sibling's idea do NOT "
    "count as mechanistically different.\n"
)


@dataclass
class JournalEntry:
    branch_id: int
    step_id: int
    status: str
    primary_metric: Optional[float]
    idea: str  # full text, no truncation


def format_journal(entries: List[JournalEntry], metric_name: str, n_recent: int = 30) -> str:
    """Flatten the most recent ``n_recent`` cross-branch entries into a textual log.

    Ideas are kept verbatim. A safety valve trims to 800 chars per idea only when
    the joined output would exceed ~30k chars (~7-8k tokens).
    """
    if not entries:
        return "(no branch activity yet)"
    recent = entries[-n_recent:]
    lines = []
    for e in recent:
        if e.primary_metric is None:
            metric_str = "N/A"
        else:
            metric_str = f"{e.primary_metric:.6f}"
        lines.append(
            f"[branch {e.branch_id} | step {e.step_id} | {e.status:<7s} | "
            f"{metric_name}={metric_str}]\n{e.idea}"
        )
    out = "\n\n".join(lines)
    if len(out) > 30_000:
        # safety valve: re-render with per-idea truncation
        lines = []
        for e in recent:
            metric_str = "N/A" if e.primary_metric is None else f"{e.primary_metric:.6f}"
            idea = e.idea if len(e.idea) <= 800 else e.idea[:800] + " […truncated…]"
            lines.append(
                f"[branch {e.branch_id} | step {e.step_id} | {e.status:<7s} | "
                f"{metric_name}={metric_str}]\n{idea}"
            )
        out = "\n\n".join(lines)
    return out


def format_sibling_block(sibling_ideas: List[tuple]) -> str:
    """Format ``[(branch_id, idea), ...]`` (full ideas, no truncation)."""
    if not sibling_ideas:
        return "(none — you are the first branch to initialise)"
    return "\n\n".join(
        f"### sibling branch {bid} — first idea\n{idea}"
        for bid, idea in sibling_ideas
    )


DANGEROUS_CODE_PATTERNS = [
    # File deletion
    (r'\bos\s*\.\s*remove\s*\(', "os.remove()"),
    (r'\bos\s*\.\s*unlink\s*\(', "os.unlink()"),
    (r'\bos\s*\.\s*rmdir\s*\(', "os.rmdir()"),
    (r'\bos\s*\.\s*removedirs\s*\(', "os.removedirs()"),
    # File move/rename
    (r'\bos\s*\.\s*rename\s*\(', "os.rename()"),
    (r'\bos\s*\.\s*replace\s*\(', "os.replace()"),
    # Recursive delete/copy via shutil
    (r'\bshutil\s*\.\s*rmtree\s*\(', "shutil.rmtree()"),
    (r'\bshutil\s*\.\s*move\s*\(', "shutil.move()"),
    (r'\bshutil\s*\.\s*copy\w*\s*\(', "shutil.copy*()"),
    # Shell execution
    (r'\bos\s*\.\s*system\s*\(', "os.system()"),
    (r'\bos\s*\.\s*popen\s*\(', "os.popen()"),
    (r'\bos\s*\.\s*exec\w*\s*\(', "os.exec*()"),
    (r'\bos\s*\.\s*spawn\w*\s*\(', "os.spawn*()"),
    # Subprocess (any subprocess call is a shell escape vector)
    (r'\bsubprocess\s*\.\s*\w+\s*\(', "subprocess.*()"),
    # Dynamic imports
    (r'\b__import__\s*\(', "__import__()"),
    # Pathlib destructive
    (r'\.\s*unlink\s*\(', ".unlink()"),
    (r'\.\s*rmdir\s*\(', ".rmdir()"),
]


class SafetyChecks:
    @staticmethod
    def _check_code_safety(code: str) -> Tuple[bool, List[str]]:
        """Check code for dangerous filesystem operations.

        Uses regex for most patterns and AST walk for exec/eval
        (to avoid false positives like model.eval()).

        Returns:
            (is_safe, list_of_violations)
        """
        violations = []

        # Regex-based checks
        for pattern, name in DANGEROUS_CODE_PATTERNS:
            if re.search(pattern, code):
                violations.append(name)

        # AST-based checks for exec/eval (avoids false positives like model.eval())
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval"):
                        violations.append(f"{node.func.id}() [line {node.lineno}]")
        except SyntaxError:
            pass  # Syntax check is done separately

        return (len(violations) == 0, violations)


class Prompts:
    def _format_metric_line(self, metric_value: Optional[float],
                            label: str = "Metric") -> str:
        """Format metric info as a single standardised line for LLM prompts.

        All agents MUST use this helper to present metric information to the
        LLM so that no agent sees more detail than another.
        """
        if metric_value is not None:
            return (f"{label}: {self.metric_name} = {metric_value} "
                    f"({self.metric_direction} is better)")
        return (f"{label}: {self.metric_name} = N/A "
                f"({self.metric_direction} is better)")

    def _build_idea_prompt(self) -> str:
        """Build the idea-generation prompt.

        Replicates the context the original Autoresearch LLM has:
        - README.md -> task_description
        - train.py -> current code (via _format_current_code)
        - results.tsv -> experiment_log formatted as table
        - Baseline results
        """
        parts = []

        # Task description (equivalent to README.md in original)
        parts.append(
            "You are an autonomous ML researcher. Your goal is to propose "
            "the next experimental idea to improve the ML algorithm.\n"
        )
        parts.append(f"## Task Description\n{self.task_description}")

        # Baseline results
        if self.baseline_primary_metric is not None:
            parts.append(
                f"\n## Baseline Results\n"
                f"{self._format_metric_line(self.baseline_primary_metric, 'Baseline')}"
            )

        # Current best metric
        if self.current_best_metric is not None:
            parts.append(
                f"\n## Current Best Metric\n"
                f"{self._format_metric_line(self.current_best_metric, 'Current Best')}"
            )

        # Experiment history (equivalent to results.tsv)
        parts.append(
            f"\n## Experiment History\n{self._format_experiment_log()}"
        )

        # Current code (equivalent to reading train.py)
        parts.append(f"\n## Current Code\n{self._format_current_code()}")

        # Instructions (from program.md philosophy)
        parts.append(
            "\n## Instructions\n"
            "Propose ONE specific experimental idea to try next. Consider:\n"
            "- What has worked and what hasn't (see Experiment History)\n"
            "- The current code state and architecture\n"
            "- Architecture changes, optimizer changes, hyperparameter tuning, "
            "training loop modifications are all fair game\n"
            "- Keep changes focused and atomic so we can evaluate the effect\n"
            "- If recent experiments have been discarded or crashed, try a "
            "different direction\n"
            "- All else being equal, simpler is better\n\n"
            "Respond with a concise description (3-5 sentences) of what to "
            "change and why. Be specific enough that the change can be "
            "implemented directly."
        )

        return "\n".join(parts)


    def _format_experiment_log(self) -> str:
        """Format experiment_log as a table (equivalent to results.tsv)."""
        if not self.experiment_log:
            return "(No experiments run yet.)"

        lines = [f"step | {self.metric_name:>10s} | status  | description"]
        lines.append("-" * 65)
        for rec in self.experiment_log:
            if rec.primary_metric is not None:
                metric_str = f"{rec.primary_metric:.6f}"
            else:
                metric_str = "       N/A"
            lines.append(
                f"{rec.step_id:4d} | {metric_str:>10s} | "
                f"{rec.status:<7s} | {rec.description[:60]}"
            )
        return "\n".join(lines)

    def _build_branch_init_prompt(
        self, branch: Branch, sibling_ideas: List[tuple]
    ) -> str:
        sibling_block = format_sibling_block(sibling_ideas)
        journal_block = format_journal(self.journal, self.metric_name)
        header = BRANCH_INIT_TEMPLATE.format(
            branch_id=branch.branch_id,
            parent_step=branch.parent_step_id,
            metric_name=self.metric_name,
            parent_metric=branch.parent_metric,
            parent_idea=branch.parent_idea,
            sibling_block=sibling_block,
            journal_block=journal_block,
        )
        body = self._build_idea_prompt_for_branch(branch)
        return header + "\n" + body


    def _build_idea_prompt_for_branch(self, branch: Branch) -> str:
        """Mirror of ``_build_idea_prompt`` but using branch-local history +
        a journal of cross-branch activity. Same overall structure to keep
        prompt format stable across branches and across phases.
        """
        parts = []
        parts.append(
            "You are an autonomous ML researcher. Your goal is to propose "
            "the next experimental idea to improve the ML algorithm.\n"
        )
        parts.append(f"## Task Description\n{self.task_description}")

        if self.baseline_primary_metric is not None:
            parts.append(
                f"\n## Baseline Results\n"
                f"{self._format_metric_line(self.baseline_primary_metric, 'Baseline')}"
            )

        if branch.best_metric is not None:
            parts.append(
                f"\n## Current Best Metric (this branch)\n"
                f"{self._format_metric_line(branch.best_metric, 'Branch Best')}"
            )

        parts.append(
            f"\n## Branch History (this branch only)\n"
            f"{self._format_branch_history(branch)}"
        )

        parts.append(
            f"\n## Cross-branch Journal (recent activity from all branches)\n"
            f"{format_journal(self.journal, self.metric_name)}"
        )

        parts.append(f"\n## Current Code\n{self._format_current_code()}")

        parts.append(
            "\n## Instructions\n"
            "Propose ONE specific experimental idea to try next. Consider:\n"
            "- What has worked and what hasn't on this branch (see Branch History)\n"
            "- What the other branches have been trying (see Cross-branch Journal)\n"
            "- The current code state and architecture\n"
            "- Architecture changes, optimizer changes, hyperparameter tuning, "
            "training loop modifications are all fair game\n"
            "- Keep changes focused and atomic so we can evaluate the effect\n"
            "- All else being equal, simpler is better\n\n"
            "Respond with a concise description (3-5 sentences) of what to "
            "change and why. Be specific enough that the change can be "
            "implemented directly."
        )
        return "\n".join(parts)


    def _format_branch_history(self, branch: Branch) -> str:
        if not branch.history:
            return "(no experiments on this branch yet.)"
        lines = [f"step | {self.metric_name:>10s} | status  | description"]
        lines.append("-" * 65)
        for rec in branch.history:
            if rec.primary_metric is not None:
                metric_str = f"{rec.primary_metric:.6f}"
            else:
                metric_str = "       N/A"
            lines.append(
                f"{rec.step_id:4d} | {metric_str:>10s} | "
                f"{rec.status:<7s} | {rec.description[:120]}"
            )
        return "\n".join(lines)

    def _build_evolution_instruction(
        self,
        parent: Program,
        inspirations: List[Program],
        top_programs: List[Program],
        iteration: int,
    ) -> str:
        """Build the CodeEditor instruction with OpenEvolve's evolutionary context.

        Replicates the semantic content from prompt/sampler.py diff_user.txt:
        - Current metrics
        - Areas for improvement
        - Evolution history (recent attempts on this island)
        - Top performers
        - Inspiration programs

        All island-scoped context (history, top performers, displayed island)
        uses ``parent.island`` — matching original openevolve/database.py
        where the whole prompt flow is anchored to parent.metadata["island"].
        """
        parts = []

        # Current solution performance
        dir_label = "higher" if self.metric_direction == "higher" else "lower"
        parts.append(
            f"## Current Solution Performance\n"
            f"{self._format_metric_line(parent.primary_metric, 'Current solution')}\n"
            f"- Generation: {parent.generation}, Island: {parent.island}"
        )

        # Baseline results
        parts.append(
            f"\n## Baseline Results\n"
            f"{self._format_metric_line(self.baseline_primary_metric, 'Baseline')}"
        )

        # Areas for improvement (auto-identified from population)
        improvement_areas = self._identify_improvement_areas(
            parent, top_programs
        )
        if improvement_areas:
            parts.append(f"\n## Areas for Improvement\n{improvement_areas}")

        # Evolution history (recent attempts on parent's island)
        history = self._build_evolution_history(parent.island)
        if history:
            parts.append(f"\n## Evolution History (Recent Attempts)\n{history}")

        # Top performers
        if top_programs:
            top_section = []
            for i, p in enumerate(top_programs):
                top_section.append(
                    f"{i+1}. [{self.metric_name}: {p.primary_metric}] "
                    f"{p.changes_description[:100]}"
                )
            parts.append(
                f"\n## Top Performers (Island {parent.island})\n"
                + "\n".join(top_section)
            )

        # Inspiration programs (diverse approaches)
        if inspirations:
            insp_section = []
            for i, p in enumerate(inspirations):
                insp_section.append(
                    f"{i+1}. [{self.metric_name}: {p.primary_metric}, Gen: {p.generation}] "
                    f"{p.changes_description[:100]}"
                )
            parts.append(
                "\n## Inspiration Programs (Diverse Approaches)\n"
                + "\n".join(insp_section)
            )

        # Instructions
        parts.append(
            "\n## Instructions\n"
            f"Improve the ML code to achieve better {self.metric_name} "
            f"({dir_label} is better).\n"
            "- Focus on a single, targeted change\n"
            "- Do not repeat approaches from Evolution History\n"
            "- Consider insights from Top Performers and Inspirations\n"
            "- Keep changes minimal and focused on one idea"
        )

        return "\n".join(parts)


    def _identify_improvement_areas(
        self, parent: Program, top_programs: List[Program]
    ) -> str:
        """Auto-identify improvement areas (from prompt/sampler.py)."""
        areas = []
        if parent.primary_metric is not None and top_programs:
            best_metric = top_programs[0].primary_metric
            if best_metric is not None and parent.primary_metric != best_metric:
                gap = abs(best_metric - parent.primary_metric)
                areas.append(
                    f"- Gap to best on this island: {gap:.6f}"
                )
        if parent.analysis:
            areas.append(f"- Last analysis: {parent.analysis[:200]}")
        return "\n".join(areas) if areas else ""


    def _build_evolution_history(self, island_id: int) -> str:
        """Build evolution history for an island (recent attempts).

        Includes feature coordinates and outcome classification to match
        original OpenEvolve's richer prompt context.
        """
        island_programs = [
            self.db.programs[pid]
            for pid in sorted(self.db.islands[island_id])
            if pid in self.db.programs
        ]
        # Sort by step_id (most recent first)
        island_programs.sort(
            key=lambda p: p.step_id if p.step_id is not None else 0,
            reverse=True,
        )

        lines = []
        for p in island_programs[:8]:
            # Status
            status = "success" if not p.is_buggy else "failed"

            # Feature coordinates (complexity/diversity bins)
            feat_key = self.db._calculate_feature_key(p)
            feat_str = f"features=({feat_key})"

            # Outcome classification vs parent
            outcome = ""
            if p.parent_id and p.parent_id in self.db.programs:
                parent_prog = self.db.programs[p.parent_id]
                if parent_prog.primary_metric is not None and p.primary_metric is not None:
                    delta = p.primary_metric - parent_prog.primary_metric
                    if self.metric_direction == "lower":
                        delta = -delta  # positive = improvement for "lower is better"
                    if delta > 0:
                        outcome = f"improved(+{abs(delta):.6f})"
                    elif delta < 0:
                        outcome = f"declined(-{abs(delta):.6f})"
                    else:
                        outcome = "stable"

            desc = p.changes_description[:100]
            metric_str = f"{self.metric_name}={p.primary_metric}"
            parts = [f"[{status}]", desc, f"| {metric_str}", feat_str]
            if outcome:
                parts.append(outcome)
            lines.append("- " + " ".join(parts))
        return "\n".join(lines) if lines else ""


    def _draft_prompt(self, parent: MCTSNode) -> str:
        """Generate a brand-new approach (child of root)."""
        memory = self.journal.build_memory_context(self.metric_direction, self.metric_name)
        instruction = (
            f"You are starting a new approach to improve the ML code for this task.\n\n"
            f"## Previous attempts summary\n{memory}\n\n"
            f"## Baseline\n{self._format_metric_line(self.baseline_primary_metric, 'Baseline')}\n\n"
            "## Instructions\n"
            "Propose AND implement a novel algorithmic improvement.\n"
            "Use the editor's requested output format."
        )

        return instruction


    def _improve_prompt(self, parent: MCTSNode) -> str:
        """Improve an existing working solution."""
        memory = self.journal.build_memory_context(self.metric_direction, self.metric_name)

        # Summarise parent's result
        parent_summary = (
            f"Parent node {parent.node_id}: "
            f"{self._format_metric_line(parent.primary_metric, 'Metric')}, "
            f"action={parent.action}"
        )
        if parent.analysis:
            parent_summary += f"\nAnalysis: {parent.analysis}"

        instruction = (
            f"You are improving an existing ML solution.\n\n"
            f"## Current solution info\n{parent_summary}\n"
            f"The current code in the target files reflects this solution's modifications.\n\n"
            f"## Previous attempts summary\n{memory}\n\n"
            "## Instructions\n"
            "Make a single, targeted improvement to boost the metric further.\n"
            "- Focus on one specific change, not multiple simultaneous modifications.\n"
            "- Do not repeat approaches that have already been tried (see previous attempts).\n"
            "Use the editor's requested output format."
        )

        return instruction
