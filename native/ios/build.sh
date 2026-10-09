#!/bin/sh
# Build the benchmark app's firmware-derived inputs into out/cfcore/ios/
# and generate the Xcode project.
#
#     native/ios/build.sh [START [CARD]]
#
# START is cfstart's file (default out/cfcore/cycles-play.start). CARD is
# the +Drive's card file, if START was made with one (cfstart --card-file;
# default out/cfcore/cycles-play.card, and none if that is not there): it
# goes in the Cycles app only, so CyclesBench cannot open such a START.
# The translated blocks are out/cfcore/aot_gen.rs, or CFCORE_AOT.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=$repo/out/cfcore/ios
start=${1:-$repo/out/cfcore/cycles-play.start}
card=${2:-$repo/out/cfcore/cycles-play.card}
export CFCORE_AOT=${CFCORE_AOT:-$repo/out/cfcore/aot_gen.rs}
# Its own target directory: this build must not wait on, or evict, the Mac's.
export CARGO_TARGET_DIR=$repo/out/cfcore/target-ios
mkdir -p "$out"
cd "$repo/native/cfcore"
cargo rustc --lib --release --target aarch64-apple-ios --crate-type staticlib
cp "$CARGO_TARGET_DIR/aarch64-apple-ios/release/libcfcore.a" "$out/"
cp "$start" "$out/cycles-play.start"
rm -f "$out/cycles-play.card"
if [ -f "$card" ]; then cp "$card" "$out/cycles-play.card"; fi
cd "$here"
xcodegen generate
