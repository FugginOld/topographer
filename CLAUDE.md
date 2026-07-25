# CLAUDE.md

**Read [CONTRIBUTING.md](CONTRIBUTING.md) first** — pipeline, commands, the full test list, and the
eight gotchas that have each broken something real. [CONTEXT.md](CONTEXT.md) is the domain glossary
(Topology, Collector, Card, Store, Widget); use those names.

Agent-specific, on top of that:

- **Commit only when asked**, straight to `main` (no feature-branch / PR flow), and end commit
  messages with the `Co-Authored-By` trailer.
- **Never tell the user to `git pull` on a reporting client, or to `systemctl` on Unraid** — see
  gotchas 7 and 8. Both are wrong on the production setup and waste a debugging round-trip.
- Match the terseness and comment density of the file you are editing; don't add a dependency for
  what a few lines of stdlib can do.
