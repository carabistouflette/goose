// Goose compiler — its version, a translation unit of its own: the header
// the build writes (cmake/version.cmake) changes with every commit, and
// only this file is compiled again for it.
#include "goose_version.h"

namespace goose {

const char *CompilerSourceRevision() { return GOOSE_SOURCE_REVISION; }

const char *CompilerVersion() { return GOOSE_VERSION; }

}  // namespace goose
