# AutoResearch experiment harness

This snapshot contains the FML search implementations, fixed-interval controller, trajectory samplers, and the AHC026 adapter.

- [Search algorithms, controller and sampling](fml_search/README.md)
- [AHC026 adapter and local judge](benchmarks/ale/README.md)
- [Organon native search providers and experiment permissions](organon/README.md)
- [FML-Bench attribution and license](fml_search/LICENSE-FML)

To check source consistency, compare `git rev-parse HEAD` with the shared commit and run `git status --short` to check for local edits. The offline Python tests use scripted models and evaluators. From the checkout root, run:

```shell
python -B -m unittest discover -s fml_search -t .
```

Live AHC026 execution uses official judge tools and task data in a separate workspace setup. The documented live module commands run from the parent directory with the checkout named `autoresearch_rebuild`. The offline checks above work with any checkout name.
