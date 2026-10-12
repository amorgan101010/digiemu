# Building the Cycles app on the Mac from files made on the PC

This is a checklist for whoever (person or coding agent) builds and installs
the Cycles app on the Mac. Do the steps in order. Do not skip a step, and do
not go on past a step whose check fails.

## What this is

The app runs a Model:Cycles firmware on Aileen's iPad and iPhone. Three
files that are made from the firmware cannot be in git, so they are made on
the PC and copied to the Mac by hand:

| File | Size | What it is |
| --- | --- | --- |
| `aot_gen.rs` | about 2 MB | The firmware's code translated to Rust. Compiled into the library. |
| `cycles-play.start` | about 52 MB | The machine's memory at the moment the app starts. |
| `cycles-play.card` | about 580 MB | The +Drive: the storage card, with 1,162 samples on it. |

The three belong together. They were made from one firmware (a modified
Model:Cycles OS 1.13 whose `.syx` has a SHA-256 starting `5a8f6095`). A
mix of old and new files does not run: the app refuses it.

Your job is only: put the three files in place, build, install, launch,
and report. Everything else is already done and checked on the PC.

## Rules

1. Never commit or push anything. This checklist needs no commit.
2. Never run `git add` on anything under `out/`, and never copy the three
   files anywhere inside the repo except `out/cfcore/`. `out/` is ignored
   by git on purpose: its contents are copyrighted firmware.
3. Never run `git reset`, `git clean`, `git stash`, `git checkout -- FILE`,
   `git push`, or any git command with `--force`.
4. Do not edit Rust files (`native/cfcore/`). If Rust does not compile, stop
   and report (see "When something fails").
5. Do not regenerate, rename the contents of, or "fix" the three files.
6. Do not delete the old files. Step 3 moves them aside.
7. If a check does not give the expected result, stop there and write the
   report at the end of this file. Do not try other approaches.

## Step 0: where you are

Every command below is run from the repository root: the folder that has
`native/`, `docs/` and `emu/` in it. Check:

```sh
ls native/ios/build.sh native/cfcore/Cargo.toml
git branch --show-current
```

Expected: both files are listed, and the branch is `cfcore-native`. If the
branch is something else, stop and report.

## Step 1: tools

```sh
xcodebuild -version
xcodegen --version
cargo --version
rustup target list --installed | grep aarch64-apple-ios
```

Expected: each prints a version, and the last prints `aarch64-apple-ios`.
If the last prints nothing, run `rustup target add aarch64-apple-ios` and
check again. If `xcodegen` is missing, run `brew install xcodegen`. Anything
else missing: stop and report.

## Step 2: get the code

```sh
git status --short
git pull --ff-only
git log --oneline -1
grep -c cfcore_open_card native/ios/Cycles/Engine.swift
```

- `git status --short` may list untracked files (lines starting `??`).
  That is fine. If it lists modified files (lines starting ` M` or `M `),
  do not undo them: write them in your report and carry on.
- If `git pull --ff-only` fails, stop and report its full output.
- The `grep` must print `3`. If it prints anything else, the pull did not
  bring this checklist's code: stop and report.

## Step 3: put the three files in place

Aileen copies the files from the PC into `~/cfcore-files/` before you
start. There are four: the three above and `SHA256SUMS`.

```sh
ls -l ~/cfcore-files/aot_gen.rs ~/cfcore-files/cycles-play.start \
      ~/cfcore-files/cycles-play.card ~/cfcore-files/SHA256SUMS
(cd ~/cfcore-files && shasum -a 256 -c SHA256SUMS)
```

Expected: four files listed, and three lines ending `: OK`. If a file is
missing or a line says `FAILED`, stop and report: the copy from the PC was
not finished, and only Aileen can redo it.

Move what is there now aside, then copy the new files in:

```sh
mkdir -p out/cfcore/before-5a8f6095
for f in aot_gen.rs cycles-play.start cycles-play.card; do
  if [ -f "out/cfcore/$f" ]; then mv "out/cfcore/$f" out/cfcore/before-5a8f6095/; fi
done
cp ~/cfcore-files/aot_gen.rs ~/cfcore-files/cycles-play.start \
   ~/cfcore-files/cycles-play.card ~/cfcore-files/SHA256SUMS out/cfcore/
(cd out/cfcore && shasum -a 256 -c SHA256SUMS)
git status --short
```

Expected: three lines ending `: OK` again, and `git status --short` shows
nothing under `out/`.

If `out/cfcore/before-5a8f6095/` already has files in it from an earlier
attempt, the `mv` is skipped for files no longer in `out/cfcore/`: that is
fine. Do not empty that folder.

## Step 4: check the files on the Mac itself

This builds the command-line tools for the Mac with the new translated
code, and runs the machine for a moment. It takes 5 to 8 minutes and
prints little while it works. Wait for it.

```sh
(cd native/cfcore && CFCORE_AOT="$PWD/../../out/cfcore/aot_gen.rs" cargo build --release)
```

Expected last line: `Finished` followed by `release profile`. Warnings are
fine. An `error` is not: stop and report.

First check, that the state opens with its card:

```sh
native/cfcore/target/release/cfpoke out/cfcore/cycles-play.start \
  --ips 211700000 --card out/cfcore/cycles-play.card run 1 card
```

Expected, exactly:

```
card: 0 commands, 0 bytes moved, 0 sectors kept here and 1133517 in its file, of 3866624
```

Second check, that a sample loads from the card and sounds. This presses
the panel's keys in the machine (it makes no sound on the Mac). Copy the
whole command, it is one line:

```sh
native/cfcore/target/release/cfpoke out/cfcore/cycles-play.start --ips 211700000 --card out/cfcore/cycles-play.card key 0 5 1 run 0.05 key 0 5 0 run 0.6 key 0 0 1 run 0.1 key 0 0 0 run 0.4 turn 3 1 run 0.3 turn 3 1 run 0.3 turn 3 1 run 0.3 turn 3 1 run 0.3 turn 3 1 run 0.3 turn 3 1 run 0.3 run 0.4 key 3 7 1 run 0.1 key 3 7 0 run 0.3 key 1 2 1 run 0.1 key 0 0 1 run 0.1 key 0 0 0 run 0.1 key 1 2 0 run 0.5 turn 3 1 run 0.3 turn 3 1 run 0.3 run 0.4 key 3 7 1 run 0.1 key 3 7 0 run 0.3 run 0.5 turn 3 1 run 0.3 run 0.4 key 3 7 1 run 0.1 key 3 7 0 run 0.3 run 0.5 turn 3 1 run 0.3 run 0.4 key 3 7 1 run 0.1 key 3 7 0 run 0.3 run 0.5 key 3 7 1 run 0.1 key 3 7 0 run 0.3 run 1 key 1 0 1 run 0.1 key 1 0 0 run 0.4 key 1 0 1 run 0.1 key 1 0 0 run 0.4 key 1 0 1 run 0.1 key 1 0 0 run 0.4 run 0.5 pad 5 110 listen 0.6 card
```

Expected, exactly these two lines:

```
peak %: 15 10 9 9 8 7 8 6 5 3 3 3
card: 36 commands, 1196032 bytes moved, 0 sectors kept here and 1133517 in its file, of 3866624
```

If either check prints something else (for example `this machine runs
another firmware than this build's translated blocks are for`, or
`its +Drive is over a card file`), the three files do not match each
other or the code. Stop and report the exact output.

## Step 5: build the iOS library and the Xcode project

```sh
native/ios/build.sh
ls -l out/cfcore/ios/libcfcore.a out/cfcore/ios/cycles-play.start out/cfcore/ios/cycles-play.card
```

This takes about 4 minutes. Expected: it ends with xcodegen saying it
created the project, and the `ls` lists all three files. The `.card` there
must be about 580 MB. If `cycles-play.card` is missing from the `ls`, step
3 was not done: stop.

## Step 6: find the team and the device

The signing team (a 10-character code). It is in the app built last time:

```sh
codesign -dv --verbose=4 out/cfcore/ios/derived/Build/Products/Release-iphoneos/Cycles.app 2>&1 | grep TeamIdentifier
```

If that prints nothing, try
`defaults read com.apple.dt.Xcode IDEProvisioningTeams | grep teamID`.
If neither gives a code, ask Aileen for the team ID. Do not guess one.

The device. Ask Aileen to plug in and unlock the iPad (or the iPhone)
first, then:

```sh
xcrun devicectl list devices
```

Take the `Identifier` of the row for the device Aileen named, whose State
is `connected` or `available (paired)`. If no device is listed that way,
stop and tell Aileen the device is not seen.

Below, `TEAM` and `UDID` stand for those two values. Put the real values
in. Never write either value into a file in the repo.

## Step 7: build the app

```sh
cd native/ios
xcodebuild -project CyclesBench.xcodeproj -scheme Cycles \
  -configuration Release -destination generic/platform=iOS \
  -derivedDataPath ../../out/cfcore/ios/derived \
  -allowProvisioningUpdates DEVELOPMENT_TEAM=TEAM build
cd ../..
du -sh out/cfcore/ios/derived/Build/Products/Release-iphoneos/Cycles.app
```

Expected: `** BUILD SUCCEEDED **`, and the app is about 640 MB. An app of
about 60 MB means the card is not in it: stop and report.

Only the `Cycles` scheme. Do not build or install `CyclesBench`: it has no
card and cannot open this state.

If the build fails:

- An error about signing or provisioning that names a device: run the
  same command once more with `-destination id=UDID
  -allowProvisioningDeviceRegistration` in place of
  `-destination generic/platform=iOS`.
- A Swift compile error in a file under `native/ios/Cycles/`: the code
  that opens the card file was written on the PC, where Swift cannot be
  compiled, so a small mistake there is possible. If the fix is a change
  of one or two lines that the compiler's message spells out (a wrong
  type, a missing label, a missing `try`), make it, build again, leave it
  uncommitted, and put the `git diff` in your report. Anything larger:
  stop and report the full error.
- Anything else: stop and report the full error.

## Step 8: install and launch

Before installing, ask Aileen to do this and wait for the answer:

> Press Home on the device so the app goes to the background, and tell me
> when that is done. (The app saves when it goes to the background.
> Installing over it while it is in front loses what was not saved.)

Then:

```sh
xcrun devicectl device install app --device UDID \
  out/cfcore/ios/derived/Build/Products/Release-iphoneos/Cycles.app
xcrun devicectl device process launch --device UDID --terminate-existing \
  io.github.amorgan101010.cycles
```

The install sends 640 MB and can take several minutes. Expected: both
commands end without an error (the install names the installed app, the
launch says it launched it). Do not add `--console` to the launch: it
never returns.

If the install is refused because the device is not in the provisioning
profile, do the first fix under step 7 and install again.

## Step 9: what Aileen should see

Ask Aileen to check these on the device and tell you the result. You
cannot check them yourself.

1. The Model:Cycles panel with its screen showing the main screen, and
   pads that make sound.
2. Press MACHINE, turn LEVEL/DATA to `Sample`, and press LEVEL/DATA.
   Then hold FUNCTION and tap MACHINE: a browser opens with folders in it
   (AK WaveForms, Aotearoa, Central Sidstation and others). Going into a
   folder and pressing LEVEL/DATA on a sample shows "Sample loaded to
   track", and the track's pad then plays it.

These are expected and are not faults:

- The first launch starts fresh. The machine the app kept from the
  earlier firmware cannot run on this one, so the app renames it
  `cycles.save.bad` (it is in the app's Documents) and starts from the
  new state. Projects saved to the +Drive under the earlier firmware are
  inside that file and do not carry over.
- The app is about 640 MB.
- CyclesBench, if it is on the device, still has the old firmware.

## When something fails

Stop at the step that failed. Do not retry with different commands, do
not edit files to get past it (except the one Swift case in step 7), and
do not undo the steps before it.

## The report

When you finish or stop, write this to Aileen:

1. The last step you completed, and the step you stopped at if any.
2. For a failure: the exact command, and its full output (not a summary).
3. The output of `git status --short` and `git log --oneline -1`.
4. The output of `git diff` if you changed any file.
5. Which device it was installed on, if it was.
6. What Aileen saw in step 9, in their words.
