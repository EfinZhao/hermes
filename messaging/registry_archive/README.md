# Registry snapshots

`tools/gen_registry.py` writes one `registry_vN.json` here for every registry version and checks
the append-only rules against them (no deleted channels, no id changing meaning, version goes up by 1).

The snapshots are git-ignored on purpose: they are your team's history, not part of the project.
Keep them however suits you (commit them in your own fork, back them up, ...). If you delete them,
the rules simply have nothing to compare against.
