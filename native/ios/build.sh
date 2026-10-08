#!/bin/sh
# Build the benchmark app's firmware-derived inputs into out/cfcore/ios/
# and generate the Xcode project.
#
#     native/ios/build.sh [START]
#
# START is cfstart's file (default out/cfcore/cycles-play.start). The
# translated blocks are out/cfcore/aot_gen.rs, or CFCORE_AOT.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=$repo/out/cfcore/ios
start=${1:-$repo/out/cfcore/cycles-play.start}
export CFCORE_AOT=${CFCORE_AOT:-$repo/out/cfcore/aot_gen.rs}
# Its own target directory: this build must not wait on, or evict, the Mac's.
export CARGO_TARGET_DIR=$repo/out/cfcore/target-ios
mkdir -p "$out"
cd "$repo/native/cfcore"
cargo rustc --lib --release --target aarch64-apple-ios --crate-type staticlib
cp "$CARGO_TARGET_DIR/aarch64-apple-ios/release/libcfcore.a" "$out/"
cp "$start" "$out/cycles-play.start"
cd "$here"
xcodegen generate
