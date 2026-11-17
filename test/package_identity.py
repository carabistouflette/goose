"""Resolved package graph qualification: native compiler, real JIT/AOT, isolated cache."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

EXE = None
CC = 'clang'
SOURCE = Path(__file__).resolve().parents[1]


def canonical_field(value):
    data = value.encode()
    return len(data).to_bytes(8, 'big') + data


class PackageIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='goose-package-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, GOOSE_HOME=str(self.root / 'cache'), GOOSE_STDLIB=str(SOURCE / 'stdlib'))
        self.graph_path = self.root / 'graph.json'
        info = json.loads(subprocess.check_output([str(EXE), 'version', '--json'], text=True))
        self.revision = info['compiler_revision']
        self.assertEqual(info['package_graph'], 'goose-package-graph-v1')

    def node(self, key, source, namespace='', bindings=None, directory=None, package=None):
        directory = directory or key
        folder = self.root / directory
        folder.mkdir(exist_ok=True)
        entry = 'entry.goose'
        (folder / entry).write_text(source)
        return {'key': key, 'source': {'locator': 'workspace:' + (package or key),
                'revision': 'snapshot:' + hashlib.sha256(source.encode()).hexdigest(), 'name': package or key, 'subdir': ''},
                'directory': directory, 'entry': entry, 'entry_namespace': namespace, 'bindings': bindings or {}, 'config': {}}

    def graph(self, nodes, root='app'):
        document = {'schema': 'goose-package-graph-v1', 'compiler_revision': self.revision, 'root': root, 'nodes': nodes}
        self.graph_path.write_text(json.dumps(document))
        return document

    def command(self, *args, ok=True, graph=True, root='app'):
        argv = [str(EXE), *args]
        if graph:
            argv += ['--package-graph', str(self.graph_path)]
        argv += [str(self.root / root / 'entry.goose')]
        result = subprocess.run(argv, env=self.env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        self.assertNotIn('AddressSanitizer', result.stderr)
        self.assertNotIn('runtime error:', result.stderr)
        return result

    def versions(self, same_shape=False):
        one = self.node('one', 'namespace versioned; struct Value { data: i64 } var counter: i64 = 0; fn make() -> Value { Value { 11 } } fn hit() -> i64 { counter++; counter }', 'versioned', package='dep')
        two = self.node('two', 'namespace versioned; struct Value { data: i64' + (' }' if same_shape else ', extra: i64 }') + ' var counter: i64 = 0; fn make() -> Value { Value { ' + ('22' if same_shape else '22, 33') + ' } } fn hit() -> i64 { counter++; counter }', 'versioned', package='dep')
        app = self.node('app', 'import old; import newer; import std; fn main() { let a = old::make(); let b = newer::make(); let x = old::hit(); let y = newer::hit(); print(a.data, " ", b.data, " ", x, " ", y); }', bindings={'old': 'one', 'newer': 'two'})
        self.graph([app, one, two])
        return app, one, two

    def test_two_versions_check_jit_and_aot(self):
        self.versions()
        self.command('check')
        self.assertEqual(self.command('run').stdout.strip(), '11 22 1 1')
        output = self.root / 'app-bin'
        self.command('build', '--cc', CC, '-o', str(output))
        self.assertEqual(subprocess.check_output([str(output)], env=self.env, text=True).strip(), '11 22 1 1')

    def test_wrong_version_nominal_types_are_rejected(self):
        app, one, two = self.versions(same_shape=True)
        (self.root / 'app/entry.goose').write_text('import old; import newer; fn take(v: newer::Value) {} fn main() { take(old::make()); }')
        result = self.command('check', ok=False)
        self.assertIn('dep@', result.stderr)
        self.assertGreaterEqual(len(set(re.findall(r'dep@([0-9a-f]{12})', result.stderr))), 2, result.stderr)

    def test_same_source_instantiated_with_different_bindings_and_generic_defaults(self):
        dep1 = self.node('dep1', 'namespace versioned; fn number() -> i64 { 1 }', 'versioned', package='dep')
        dep2 = self.node('dep2', 'namespace versioned; fn number() -> i64 { 2 }', 'versioned', package='dep')
        code = 'namespace client; import dep; fn answer(x: i64 = dep::number()) -> i64 { x } fn generic<T>(x: T) -> i64 { dep::number() + x }'
        a = self.node('a', code, 'client', {'dep': 'dep1'}, directory='client', package='client')
        b = copy.deepcopy(a)
        b['key'], b['bindings'] = 'b', {'dep': 'dep2'}
        app = self.node('app', 'import a; import b; import dep; import std; fn main() { print(a::answer(), " ", b::answer(), " ", a::generic(10), " ", b::generic(10)); }', bindings={'a': 'a', 'b': 'b', 'dep': 'dep2'})
        self.graph([dep2, b, app, a, dep1])
        self.assertEqual(self.command('run').stdout.strip(), '1 2 11 12')

    def test_two_aliases_of_one_instance_share_globals(self):
        dep = self.node('dep', 'namespace versioned; var counter: i64 = 0; fn hit() -> i64 { counter++; counter }', 'versioned')
        duplicate = copy.deepcopy(dep)
        duplicate['key'] = 'alias-node'
        app = self.node('app', 'import a; import b; import std; fn main() { let x = a::hit(); let y = b::hit(); print(x, " ", y); }', bindings={'a': 'dep', 'b': 'alias-node'})
        self.graph([dep, app, duplicate])
        self.assertEqual(self.command('run').stdout.strip(), '1 2')

    def test_owned_global_scope_initialization_and_methods(self):
        low = self.node('low', 'let seed: i64 = 17; struct Value { x: i64 } fn number(v: Value) -> i64 { v.x + seed }')
        high = self.node('high', 'namespace client; import low; let saved: i64 = low::seed; fn value() -> i64 { low::Value { saved }.number() }', 'client', {'low': 'low'})
        app = self.node('app', 'import high; import std; let seed: i64 = 99; fn main() { print(high::value()); }', bindings={'high': 'high'})
        self.graph([high, app, low])
        self.assertEqual(self.command('run').stdout.strip(), '34')

    def test_qualified_global_is_package_local(self):
        dep = self.node('dep', 'let seed: i64 = 7; import .other;')
        (self.root / 'dep/other.goose').write_text('namespace other; fn answer() -> i64 { ::seed }')
        app = self.node('app', 'import dep; import std; let seed: i64 = 99; fn main() { print(dep::other::answer()); }', bindings={'dep': 'dep'})
        self.graph([app, dep])
        self.assertEqual(self.command('run').stdout.strip(), '7')

    def test_ambiguous_alias_and_legacy_sourced_import_are_rejected(self):
        dep = self.node('dep', 'namespace library; fn answer() -> i64 { 7 }', 'library')
        app = self.node('app', 'import .collision; import dep; fn main() { dep::answer(); }', bindings={'dep': 'dep'})
        (self.root / 'app/collision.goose').write_text('namespace dep; fn local() {}')
        self.graph([app, dep])
        self.assertIn('ambiguous', self.command('check', ok=False).stderr)
        (self.root / 'app/entry.goose').write_text('import dep from "../dep"; fn main() {}')
        self.assertIn('must omit sources', self.command('check', ok=False).stderr)

    @unittest.skipIf(os.name == 'nt', 'symlink permission is platform dependent')
    def test_local_module_symlink_cannot_escape_package(self):
        app = self.node('app', 'import stolen; fn main() {}')
        outside = self.root / 'outside.goose'
        outside.write_text('fn hidden() {}')
        (self.root / 'app/stolen.goose').symlink_to(outside)
        self.graph([app])
        self.assertIn('escapes its package', self.command('check', ok=False).stderr)

    def test_package_globals_do_not_leak_from_application(self):
        dep = self.node('dep', 'namespace versioned; fn value() -> i64 { secret }', 'versioned')
        app = self.node('app', 'import dep; let secret: i64 = 99; fn main() { dep::value(); }', bindings={'dep': 'dep'})
        self.graph([app, dep])
        self.assertIn('secret', self.command('check', ok=False).stderr)

    def test_explicit_dependency_namespace_and_unnamespaced_dependency(self):
        dep = self.node('dep', 'namespace first; fn value() -> i64 { 7 } import .second;', 'first')
        (self.root / 'dep/second.goose').write_text('namespace second; fn value() -> i64 { 8 }')
        bare = self.node('bare', 'fn value() -> i64 { 9 }')
        app = self.node('app', 'import dep; import bare; import std; fn main() { print(dep::value(), " ", dep::second::value(), " ", bare::value()); }', bindings={'dep': 'dep', 'bare': 'bare'})
        self.graph([app, dep, bare])
        self.assertEqual(self.command('run').stdout.strip(), '7 8 9')

    def test_stable_full_fingerprints_match_independent_sha256(self):
        app, one, two = self.versions()
        output = self.root / 'out.c'
        self.command('-O0', '-o', str(output))
        code = output.read_text()
        def identity(node):
            source = node['source']
            fields = ['goose-package-instance-v1', self.revision, source['locator'], source['revision'], source['name'], source['subdir'], node['entry'], node['entry_namespace']]
            self.assertEqual(node['bindings'], {})
            return hashlib.sha256(b''.join(canonical_field(value) for value in fields)).hexdigest()
        for node in (one, two):
            self.assertIn('gp_' + identity(node) + '_', code)
        self.graph([two, one, app])
        self.command('-O0', '-o', str(output))
        shuffled = output.read_text()
        self.assertEqual(set(re.findall(r'gp_[a-f0-9]{64}_\w+', code)), set(re.findall(r'gp_[a-f0-9]{64}_\w+', shuffled)))
        # Relocating materialized source is not a change in package identity.
        shutil.move(self.root / 'one', self.root / 'relocated')
        one['directory'] = 'relocated'
        self.graph([app, one, two])
        self.command('-O0', '-o', str(output))
        self.assertIn('gp_' + identity(one) + '_', output.read_text())

    def test_invalid_graphs_fail_before_native_build(self):
        app, one, two = self.versions()
        document = self.graph([app, one, two])
        mutations = [lambda d: d.update(schema='future'),
                     lambda d: d.update(compiler_revision='0' * 40),
                     lambda d: d['nodes'][0]['bindings'].update(old='missing'),
                     lambda d: d['nodes'][1]['bindings'].update(loop='one'),
                     lambda d: d['nodes'][1].update(entry='../escape.goose'),
                     lambda d: d['nodes'][1].update(entry_namespace='wrong'),
                     lambda d: d['nodes'][1].update(config={'feature': True}),
                     lambda d: d['nodes'][0]['bindings'].update(std='one'),
                     lambda d: d['nodes'][0].update(unknown=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = copy.deepcopy(document)
                mutate(bad)
                self.graph_path.write_text(json.dumps(bad))
                self.command('check', ok=False)
        self.graph_path.write_text(json.dumps(document).replace('"root": "app"', '"root": "app", "root": "app"'))
        self.assertIn('duplicate', self.command('check', ok=False).stderr)
        self.graph_path.write_text('[' * 66 + '0' + ']' * 66)
        self.assertIn('resource limit', self.command('check', ok=False).stderr)
        self.graph_path.write_bytes(b'"\xff"')
        self.assertIn('UTF-8', self.command('check', ok=False).stderr)
        self.assertFalse((self.root / 'cache/build/pkg').exists())

    def test_native_and_lossy_tooling_are_rejected(self):
        app, one, two = self.versions()
        (self.root / 'one/entry.build').write_text('c_sources = ["native.c"];')
        self.assertIn('native configuration', self.command('check', ok=False).stderr)
        (self.root / 'one/entry.build').unlink()
        (self.root / 'one/entry.goose').write_text('namespace versioned; extern fn value() -> i64 = "shared_symbol";')
        self.assertIn('foreign extern/export', self.command('check', ok=False).stderr)
        self.versions()
        self.assertIn('not supported', self.command('--dump', ok=False).stderr)
        self.assertIn('not supported', self.command('--roundtrip', ok=False).stderr)
        self.assertIn('not supported', self.command('--tokens', ok=False).stderr)
        self.assertIn('not supported', self.command('toolchain', ok=False).stderr)

    def test_debug_native_build(self):
        self.versions()
        output = self.root / ('debug.exe' if os.name == 'nt' else 'debug')
        self.command('build', '--debug', '--cc', CC, '-o', str(output))
        self.assertEqual(subprocess.check_output([str(output)], env=self.env, text=True).strip(), '11 22 1 1')

    def test_lsp_rejects_graph_configuration(self):
        self.versions()
        self.assertIn('not supported', self.command('lsp', ok=False).stderr)
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'initializationOptions': {'packageGraph': str(self.graph_path)}}},
            {'jsonrpc': '2.0', 'method': 'workspace/didChangeConfiguration',
             'params': {'settings': {'goose': {'packageGraph': str(self.graph_path)}}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'shutdown'},
            {'jsonrpc': '2.0', 'method': 'exit'}]
        wire = b''
        for message in messages:
            data = json.dumps(message).encode()
            wire += b'Content-Length: %d\r\n\r\n' % len(data) + data
        result = subprocess.run([str(EXE), 'lsp'], input=wire, env=self.env,
                                capture_output=True, timeout=30)
        self.assertIn(b'resolved package graphs are not supported', result.stdout)
        self.assertIn(b'-32602', result.stdout)
        self.assertIn(b'configuration rejected', result.stdout)

    def test_legacy_namespaces_and_package_programs(self):
        for fixture in ('test/syntax/namespaces.goose', 'test/packages/pkg_basic.goose'):
            output = self.root / ('legacy.exe' if os.name == 'nt' else 'legacy')
            result = subprocess.run([str(EXE), 'build', '--cc', CC, '-o', str(output), str(SOURCE / fixture)], env=self.env,
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(output)], env=self.env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = SOURCE / 'test/expected' / (Path(fixture).stem + '.out')
            self.assertEqual(result.stdout, expected.read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', required=True, type=Path)
    parser.add_argument('--cc', default='clang')
    args, remaining = parser.parse_known_args()
    EXE, CC = args.exe.resolve(), args.cc
    unittest.main(argv=[__file__, *remaining])
