// Goose compiler — resolved, materialized package graph. Acquisition and version
// solving belong to the producer. This reader performs no subprocess or network I/O.
#pragma once
namespace goose {
struct GraphPackage {
    string key, locator, revision, name, subdir, directory, entry, entry_namespace;
    map<string, int> bindings;
    map<string, string> targets;
    string identity, canonical;
};
struct PackageGraph {
    vector<GraphPackage> nodes;  // Deduplicated, root first; other instances identity-sorted.
    string compiler_revision;
};
[[noreturn]] inline void GraphError(const string &message) {
    throw CompileError { cat("package graph: ", message) };
}
inline void GraphKeys(const Json &object, initializer_list<string_view> allowed) {
    if (object.kind != Json::OBJECT) GraphError("expected an object");
    for (auto &[key, value] : object.obj)
        if (find(allowed.begin(), allowed.end(), key) == allowed.end()) GraphError(cat("unknown field ", key));
}
inline string GraphString(const Json &object, const string &key, bool empty = false) {
    auto value = object.Get(key);
    if (!value || value->kind != Json::STRING || (!empty && value->str.empty()) || value->str.size() > 4096)
        GraphError(cat("invalid string for ", key));
    for (auto c : value->str) if ((uint8_t)c < 32 || c == 127) GraphError(cat("control character in ", key));
    return value->str;
}
inline bool GraphIdentifier(string_view text) {
    if (text.empty() || text.size() > 128) return false;
    for (size_t i = 0; i < text.size(); ++i) {
        auto c = text[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_' || (i && c >= '0' && c <= '9'))) return false;
    }
    for (int t = T_IMPORT; t <= T_TF64; ++t) if (text == TName((TType)t)) return false;
    return true;
}
inline bool GraphRelative(string_view path, bool empty = false) {
    if (path.empty()) return empty;
    if (path.size() > 1024 || path.front() == '/' || path.find('\\') != string_view::npos || path.find(':') != string_view::npos) return false;
    size_t begin = 0;
    for (size_t at = 0; at <= path.size(); ++at) if (at == path.size() || path[at] == '/') {
        auto part = path.substr(begin, at - begin);
        if (part.empty() || part == "." || part == "..") return false;
        begin = at + 1;
    }
    return true;
}
inline bool GraphHex(string_view value, size_t size) {
    if (value.size() != size) return false;
    for (auto c : value) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    return true;
}
inline string GraphPath(const string &path) {
    std::error_code ec;
    auto canonical = filesystem::weakly_canonical(filesystem::absolute(path, ec), ec);
    if (ec) GraphError("cannot canonicalize a materialization path");
    return ForwardSlashes(canonical.generic_string());
}
inline bool GraphContains(const string &root, const string &path) {
    auto prefix = root.back() == '/' ? root : root + '/';
    return path.rfind(prefix, 0) == 0;
}
inline bool GraphUtf8(string_view text) {
    for (size_t i = 0; i < text.size();) {
        auto c = (uint8_t)text[i++];
        if (c < 128) { if (!c) return false; continue; }
        unsigned value, count, minimum;
        if (c >= 0xc2 && c <= 0xdf) { value = c & 31; count = 1; minimum = 128; }
        else if (c >= 0xe0 && c <= 0xef) { value = c & 15; count = 2; minimum = 2048; }
        else if (c >= 0xf0 && c <= 0xf4) { value = c & 7; count = 3; minimum = 65536; }
        else return false;
        if (count > text.size() - i) return false;
        while (count--) { auto b = (uint8_t)text[i++]; if ((b & 0xc0) != 0x80) return false; value = (value << 6) | (b & 63); }
        if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
    }
    return true;
}
inline PackageGraph ReadPackageGraph(const string &file, const string &rootpath, const string &compiler_revision) {
    string input;
    // Bound before allocation; LoadSource handles source BOM but not graph metadata.
    std::error_code ec;
    auto size = filesystem::file_size(file, ec);
    if (ec || size > 1048576 || !LoadSource(file, input) || !GraphUtf8(input)) GraphError("unreadable, oversized, or invalid UTF-8 graph (limit 1 MiB)");
    JsonReader reader { input.c_str(), input.c_str(), true };
    auto document = reader.Value(); reader.Space(); if (*reader.p) reader.Fail("text after graph");
    GraphKeys(document, {"schema", "compiler_revision", "root", "nodes"});
    if (GraphString(document, "schema") != "goose-package-graph-v1") GraphError("unsupported schema");
    if (GraphString(document, "compiler_revision") != compiler_revision || !GraphHex(compiler_revision, 40)) GraphError("compiler revision mismatch");
    auto root = GraphString(document, "root");
    auto array = document.Get("nodes");
    if (!array || array->kind != Json::ARRAY || array->arr.empty() || array->arr.size() > 256) GraphError("expected 1..256 package nodes");
    vector<GraphPackage> parsed;
    map<string, int> keys;
    auto base = DirOf(GraphPath(file));
    for (auto &node : array->arr) {
        GraphKeys(node, {"key", "source", "directory", "entry", "entry_namespace", "bindings", "config"});
        GraphPackage p;
        p.key = GraphString(node, "key");
        if (!keys.emplace(p.key, (int)parsed.size()).second) GraphError("duplicate node key");
        auto source = node.Get("source"); if (!source) GraphError("missing source");
        GraphKeys(*source, {"locator", "revision", "name", "subdir"});
        p.locator = GraphString(*source, "locator"); p.revision = GraphString(*source, "revision");
        p.name = GraphString(*source, "name"); p.subdir = GraphString(*source, "subdir", true);
        auto revision = string_view(p.revision);
        if (!GraphHex(revision, 40) && !(revision.substr(0,7) == "sha256:" && GraphHex(revision.substr(7),64)) &&
            !(revision.substr(0,9) == "snapshot:" && GraphHex(revision.substr(9),64))) GraphError("source revision must be a full commit or SHA-256 snapshot/archive digest");
        if (!GraphRelative(p.subdir, true)) GraphError("invalid source subdirectory");
        auto directory = GraphString(node, "directory");
        p.directory = GraphPath(IsAbsoluteSource(directory) ? directory : base + directory);
        p.entry = GraphString(node, "entry"); p.entry_namespace = GraphString(node, "entry_namespace", true);
        if (!GraphRelative(p.entry) || p.entry.size() < 7 || p.entry.substr(p.entry.size()-6) != ".goose") GraphError("invalid entry path");
        if (!p.entry_namespace.empty() && !GraphIdentifier(p.entry_namespace)) GraphError("invalid entry namespace");
        auto entry = GraphPath(p.directory + '/' + p.entry);
        if (!GraphContains(p.directory, entry) || !filesystem::is_regular_file(entry, ec)) GraphError("missing entry or entry escapes its package directory");
        auto config = node.Get("config");
        if (!config || config->kind != Json::OBJECT || !config->obj.empty()) GraphError("only empty compilation config is supported by graph v1");
        auto bindings = node.Get("bindings");
        if (!bindings || bindings->kind != Json::OBJECT || bindings->obj.size() > 256) GraphError("invalid dependency bindings");
        for (auto &[alias, target] : bindings->obj) {
            if (!GraphIdentifier(alias) || alias == "std" || alias == "os" || target.kind != Json::STRING || target.str.empty()) GraphError("invalid or reserved dependency alias");
            p.targets.emplace(alias, target.str);
        }
        parsed.push_back(std::move(p));
    }
    if (!keys.count(root)) GraphError("missing root node");
    for (auto &p : parsed) for (auto &[alias, target] : p.targets) {
        auto found = keys.find(target); if (found == keys.end()) GraphError(cat("missing dependency node ", target));
        p.bindings.emplace(alias, found->second);
    }
    vector<int> state(parsed.size(), 0);
    function<void(int)> fingerprint = [&](int i) {
        if (state[i] == 2) return;
        if (state[i] == 1) GraphError("dependency cycle");
        state[i] = 1; auto &p = parsed[i];
        string canonical;
        for (auto &field : {string("goose-package-instance-v1"), compiler_revision, p.locator, p.revision, p.name, p.subdir, p.entry, p.entry_namespace}) PackageIdentityField(canonical, field);
        for (auto &[alias, target] : p.bindings) { fingerprint(target); PackageIdentityField(canonical, alias); PackageIdentityField(canonical, parsed[target].identity); }
        p.canonical = std::move(canonical); p.identity = PackageSha256(p.canonical); state[i] = 2;
    };
    for (int i = 0; i < (int)parsed.size(); ++i) fingerprint(i);
    auto root_id = parsed[keys[root]].identity;
    if (GraphPath(parsed[keys[root]].directory + '/' + parsed[keys[root]].entry) != GraphPath(rootpath)) GraphError("root source does not match graph entry");
    map<string, int> instances;
    vector<int> order;
    for (int i = 0; i < (int)parsed.size(); ++i) order.push_back(i);
    sort(order.begin(), order.end(), [&](int a, int b) {
        if ((parsed[a].identity == root_id) != (parsed[b].identity == root_id)) return parsed[a].identity == root_id;
        return parsed[a].identity < parsed[b].identity;
    });
    PackageGraph graph; graph.compiler_revision = compiler_revision;
    for (auto i : order) {
        auto &p = parsed[i]; auto previous = instances.find(p.identity);
        if (previous != instances.end()) {
            auto &existing = graph.nodes[previous->second];
            if (existing.canonical != p.canonical) GraphError("SHA-256 identity collision");
            if (existing.directory != p.directory) GraphError("one instance has conflicting materialization directories");
        } else { instances.emplace(p.identity, (int)graph.nodes.size()); graph.nodes.push_back(p); }
    }
    for (auto &p : graph.nodes) for (auto &[alias, target] : p.bindings) target = instances.at(parsed[target].identity);
    return graph;
}
}  // namespace goose
