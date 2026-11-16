# Experimental resolved package graphs

This branch adds an opt-in pure-Goose compilation mode. It is a compiler
interface for a package manager that has already resolved and materialized its
dependencies. It does not provide a registry, version solver or downloader.
Legacy compilation without this option retains the root-owned flat package
model described in [packages.md](packages.md).

```sh
goose version --json
goose check app/entry.goose --package-graph graph.json
goose run app/entry.goose --package-graph graph.json
goose build app/entry.goose --package-graph graph.json --cc clang -o app-bin
```

`version --json` reports the full compiler Git revision, runtime identity and
graph capability flags. A producer must use the exact qualified compiler build;
the revision string alone does not attest that a checkout was clean. Graph v1
accepts at most 256 nodes and a 1 MiB strict UTF-8 JSON document, with nesting
limited to 64 and 100,000 JSON values. Unknown fields are errors.

## Materialized graph

Replace the compiler revision and snapshot digests below with real values.
Directories are absolute or relative to the graph file. Entries and source
subdirectories are normalized relative paths; entry symlinks must stay within
the package directory.

```json
{
  "schema": "goose-package-graph-v1",
  "compiler_revision": "<full 40-character compiler commit>",
  "root": "app",
  "nodes": [
    {
      "key": "app",
      "source": {
        "locator": "workspace:app",
        "revision": "snapshot:<64 lowercase hex characters>",
        "name": "app",
        "subdir": ""
      },
      "directory": "app",
      "entry": "entry.goose",
      "entry_namespace": "",
      "bindings": {"old": "dep-one", "newer": "dep-two"},
      "config": {}
    }
  ]
}
```

The example omits the two dependency nodes for brevity; every target must exist.
`key` is a graph-local reference. `source` is an opaque provenance tuple:
locator, immutable revision, package name (which may contain `author/project`),
and source subdirectory. Revisions are full 40-character commits,
`sha256:<64 hex>` archive digests, or `snapshot:<64 hex>` local snapshot digests.
The producer verifies acquisition, source provenance and snapshot freshness.
**The compiler does not recompute these source digests.** This graph is a trusted
producer interface, not a source integrity or sandbox boundary. The compiler
does validate graph consistency, cycles, entry existence and import containment.

Only empty `config` is supported. Bindings map valid Goose identifier aliases
to graph keys; `std` and `os` are reserved. A package entry's declared namespace
must match `entry_namespace`. No dependency declarations are added to source:

```goose
import old;
import newer;
fn main() {
    let a = old::make();
    let b = newer::make();
}
```

Each import resolves in its defining package. `old::make` names the target's
entry namespace; `old::other::make` names an explicitly imported target
namespace. Local modules retain their package scope. A local module or namespace
colliding with an alias is rejected. Application declarations are not visible
inside dependencies. Generic bodies and defaults retain their definition scope.
Module imports, as before, determine initialization order; declaring a binding
does not execute an unimported package.

## Identity and generated symbols

Identity is SHA-256 of a length-delimited canonical record. Each field is an
eight-byte big-endian UTF-8 byte length followed by the bytes. Initial fields:

1. `goose-package-instance-v1`
2. Compiler source revision
3. Source locator, revision, name and subdirectory
4. Entry path and entry namespace

Append each outgoing alias followed by its target's full identity, in bytewise
alias order. Compute bottom-up over the acyclic graph. Config is fixed empty in
v1; introducing effective configuration requires a new schema/identity contract.
Physical directories, graph row order, node keys and incoming aliases do not
contribute. The full canonical records are compared when digests coincide.
One identity cannot claim different materialization directories in one graph.

Two aliases of one instance share its declarations and globals. The same source
with different outgoing bindings creates distinct instances, even in one
physical directory. Compilation units are keyed by package instance and path.
Nominal types from distinct instances are different types. Diagnostics display
`package@<12 hex display fingerprint>::namespace::name`; display prefixes are
not machine identity.

Generated names gain `gp_<full 64-character identity>_` before the existing
namespace/name mangling. This isolates packages; existing collision checks,
specialization numbering and optimizer traversal remain in use. This is not a
stable C ABI or a promise of byte-identical generated C across arbitrary edits.

## Deliberate first-stage limits

Non-toolchain native sidecars, foreign externs and C exports are rejected before
native compilation or JIT loading. The compiler-owned runtime and standard
library remain shared. Arbitrary C definitions/includes, graph dumps,
roundtrips, documentation dumps, export headers, token dumps and multi-test
mode are rejected. Debug native builds are supported. Sourced imports are
rejected: the producer must supply bindings and normalized source without
source declarations. Existing unmodified sourced-import packages therefore
require a later producer/compiler migration before using this mode.

LSP graph mode is unsupported and explicitly rejected by the CLI and by the
`packageGraph` initialization/configuration option. Editors must not claim graph analysis
using a legacy session. Native-provider ownership, graph-aware tooling, richer
configuration and package-manager integration are subsequent work. This branch
does not establish Cargo parity or change Honk's compiler pin.

## Qualification

```sh
python3 test/package_identity.py --exe build/goose --cc clang -v
```

The harness uses isolated caches, real typechecking, JIT and native builds,
cross-version type rejection, globals, definition-site generic/default scopes,
independent Python SHA-256 checks, malformed graphs and legacy fixtures.
Run the repository's existing test runner separately for legacy compatibility.
