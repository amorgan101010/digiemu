# Mac build blocker: CoreDevice timeout

Date: 2026-10-12
Branch at time of report: `cfcore-native`

## Build progress

Steps 0–5 of `native/ios/MAC-BUILD.md` passed. The firmware files in `out/cfcore/` passed their SHA-256 checks. The Mac `cfpoke` card checks matched the expected output. `native/ios/build.sh` returned `EXIT 0`, created `CyclesBench.xcodeproj`, and produced `libcfcore.a`, `cycles-play.start`, and the 580,374,000-byte `cycles-play.card`.

The signing team found in the previous app bundle is `VKRFD76RCD`. The iPad was plugged in and unlocked. No app was built, installed, or launched; the process stopped before Step 7 because Step 6 could not enumerate the device.

## Reproducible failure

`xcrun devicectl list devices` timed out four times: initially, after confirming the iPad was plugged in, after restarting `CoreDeviceService`, and after restarting the Mac. Each run returned exit code 1 and this same output:

```text
ERROR: Timed out waiting for CoreDeviceService to fully initialize. This is likely a bug in CoreDevice. Please file a bug report against CoreDevice | X. (com.apple.coredevice.devicectl error 1 (0x01))
```

The restart command `killall -9 CoreDeviceService` succeeded when run with system-process access. Waiting 15 seconds and listing devices again produced the same timeout. The Mac was then restarted; the next list attempt also produced the same timeout.

## Xcode launch observation

The command-line toolchain reports:

```text
Xcode 27.0
Build version 27A266a
```

`xcode-select -p` reports `/Applications/Xcode.app/Contents/Developer`. However, launching the GUI by application name failed:

```text
Command: open -a Xcode
Unable to find application named 'Xcode'
```

Launching the bundle by path also failed:

```text
Command: open /Applications/Xcode.app
The application cannot be opened for an unexpected reason, error=Error Domain=NSOSStatusErrorDomain Code=-10827 "kLSNoExecutableErr: The executable is missing" UserInfo={_LSLine=4288, _LSFunction=_LSOpenStuffCallLocal, _LSFile=LSOpenCore.mm, _LSErrorMessage=kLSNoExecutableErr}
```

The bundle path exists, `Contents/Info.plist` names `Xcode` as `CFBundleExecutable`, and `Contents/MacOS/Xcode` exists as an arm64 Mach-O executable. The reason LaunchServices refuses to open it is unknown.

## Request for diagnosis

Please advise how to diagnose the mismatch between the selected Xcode command-line toolchain and the GUI app launch failure, and how to restore CoreDevice enumeration safely. In particular, is this a damaged/unregistered Xcode app bundle, a CoreDevice/Xcode 27 issue, or something else? The next build/install step depends on `devicectl` returning the connected iPad's identifier.
