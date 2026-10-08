# Model:Cycles on iPad: implementation plan

Prepared 2026-10-07 from this checkout. This is a proposed port, not a claim
that an iPad build has been tested.

The target is a universal iPad/iPhone application that runs the Model:Cycles
firmware, panel, sequencer, storage and audio entirely on each device. The
MacBook builds and installs it. Start with Model:Cycles OS 1.13 only.

## Chosen direction: private ahead-of-time build, independent launch

The user prefers independent launch every time and has an approximately
2020 M1 MacBook Air for building. The target iPad is A1954, identified by
[Apple](https://support.apple.com/en-au/108043) as the sixth-generation
Wi-Fi + Cellular iPad. Its [specifications](https://support.apple.com/en-us/111957)
list an A10 Fusion CPU, 9.7-inch display, Lightning and a headphone jack.
The user also has an [iPhone 13 with A15 Bionic](https://support.apple.com/en-us/111872).
Both devices run their latest supported OS, according to the user. Apple's
[release list](https://support.apple.com/en-us/100100), checked 2026-10-07,
lists iOS 27.0.1 for iPhone 11 and later and iPadOS 17.7.11 as its newest
entry for the sixth-generation iPad. Treat those as planning assumptions;
record actual build numbers when testing. Mac macOS/Xcode versions remain
to be confirmed.

Use the iPhone 13 for the first independent-launch/audio proof, then run
the identical workload on the A10 iPad immediately. The A15 offers a more
promising performance target, but neither device has measured results.
The iPad remains a required target: phone success does not establish iPad
viability. Report throughput, latency, fallback time, memory and thermal
results separately. Both run locally; the phone is not an audio server for
the iPad. Share the engine and use a universal app with tablet and phone
layouts, retaining an iPadOS 17-compatible deployment target and API usage.
The user reports an MM/MD iPhone port using
precompiled DSP code, but its source is unavailable; treat it as motivation,
not verified implementation evidence.

Make ahead-of-time compilation (AOT) the primary feasibility investigation:
translate the user's fixed Model:Cycles firmware on the Mac, compile the
result into the signed iPad app, and execute it without runtime code
creation. Pair compiled blocks with a correct no-JIT interpreter for paths
that were not compiled. Ordinary firmware data can remain in guest memory;
the translated host code must be built and signed before installation.

The current patched Unicorn 2.1.4 engine uses JIT. Apple's
[platform security guide](https://support.apple.com/guide/security/security-of-runtime-process-sec15bfe098e/web)
documents restrictions on executable memory. The macOS allow-JIT entitlement
is not a solution for this deliverable. Retain Unicorn on the desktop as a
reference implementation; an iPad JIT experiment is optional diagnostic work,
not a milestone required by this plan.

The important architectural distinction: Cycles runs its OS, sequencer and
synthesis on the emulated ColdFire CPU. A DSP56300 precompiler from an MM/MD
project would not directly translate this firmware. Precompiling synthesis
alone is viable only if the remaining ColdFire execution uses a sufficiently
fast interpreter. AOT plus interpreter fallback must cover the entire machine.

The deliverable is a reproducible private build pipeline. Keep firmware and
all generated firmware-derived sources/objects/binaries outside the public
repository. A different firmware requires regeneration and rebuilding the
app; ordinary projects and sounds should remain runtime data. This plan
makes no determination about rights to redistribute generated artifacts.

## Measured on the desktop (2026-10-07)

Model:Cycles OS 1.13, `gui.snap`, factory pattern playing after PLAY, on a
Ryzen 5 5600G under Linux with the six-patch Unicorn. Headless `gui.Emulator`
with a stub sound card, two runs that agree. Nothing here was run on an
Apple device.

| Measure | Value |
| --- | --- |
| Unpaced capacity | 1.18x real time (paced: 100%, no underruns) |
| Host time (`perf`, user cycles, unpaced) | Python interpreter 60%, Unicorn library 24%, JIT-generated code 13%, libc 2% |
| Largest Python costs (inclusive) | `dtim.Timers.service` 16%, `pit.Pits.service` 13%, device hook callbacks 11%, `Machine.raise_vector` 9%, `ssi.Ssi0Dma.service` 7% |
| Guest instructions | 108M per emulated second, counted exactly by the replay below (8.5M block entries; earlier estimates here of 100M and 125M were from a disassembler that does not decode EMAC and from the render's 14.8 instructions a block applied to every block) |
| Blocks | 8.5M entries per emulated second, 2,570 distinct |
| Hot code | 14 blocks are 50% of instructions, 109 are 90%, 576 are 99% (21 KB of code), 1,104 are 99.9% |
| Where | six 4 KB pages hold 93%: 0x40056000-0x40058fff and 0x400a7000-0x400a9fff |
| Code source | every block executed in the window (2,560 in the check run) lies inside the loaded OS image and its bytes in memory equal `section_3_MAIN_OS.bin`: nothing ran from copied or patched code |

What follows from it:

- The desktop has little to spare: of 0.85 s of CPU per emulated second,
  about 0.52 s is Python and 0.32 s the engine. The Cycles is heavier than
  the "100% of real time" in MODELS.md suggests, and most of that weight is
  the Python device models, not the emulated CPU.
- [C] An earlier version of this section gave engine 55%, Python 45%, from
  `tools/capbench.py`'s stack sampler. That sampler runs on another Python
  thread and only gets the GIL while the emulator thread is inside
  `emu_start`, so it over-counts the engine. The `perf` figures replace it,
  and they are consistent with the engine's speed measured alone below
  (108M instructions at about 400 MIPS would be 0.27 s).
- Python cannot stay on the audio path on the A10. At roughly half the
  desktop's single-thread speed (an estimate, not measured), today's Python
  share alone is about 1.0 s per second. The timers, interrupt delivery,
  SSI/eDMA and the device hooks have to be native for the iPad; step 2's
  "avoid a wholesale rewrite" holds for boot, storage and UI glue, not for
  the per-step path.
- An interpreter as the main engine is unlikely on the A10 and uncertain on
  the A15 (an estimate): it would have to run about 108M EMAC-heavy guest
  instructions a second with headroom. Unicorn 2.1.4 has no interpreter build (no TCI in its
  `qemu/tcg`), so there is no quick no-JIT baseline to measure.
- The A15 is roughly the desktop's single-thread speed (an estimate), so
  with today's Python it would sit near 1.2x even if a JIT were allowed. The
  per-step path needs to be native on the phone as well.
- Ahead-of-time translation fits the workload: 99.9% of the instructions
  are about 1,100 blocks, so the fallback interpreter can be simple and slow.
- The budget for the A10, on the same half-speed estimate: 1.3x real time
  is 0.77 s of CPU per emulated second. An engine as fast as Unicorn's JIT
  would take about 0.65 s of it, leaving 0.12 s for every device. cfcore
  at the 0.22 s measured below on x86-64 would take about 0.44 s and leave
  0.33 s, which is 0.16 s of desktop time for work that costs 0.52 s in
  Python today.
- Measured only with the factory pattern playing; capacity while stopped or
  with a denser pattern was not measured.

## Translated code against Unicorn's JIT (2026-10-07)

`native/cfcore` is a first Rust ColdFire/EMAC core: an interpreter, and
blocks translated to Rust ahead of time from the firmware image, both built
from the same instruction handlers ([its README](../native/cfcore/README.md)
has the method). Measured on the same Ryzen 5 5600G, x86-64 only:

- **Workload.** 40 stretches of the playing Cycles between two device
  accesses (such stretches are 96% of the code it runs), 7,000 to 53,000
  instructions each, 1.04M in all, captured with the six-patch Unicorn's
  final registers, flags, EMAC state and memory.
- **Correct.** The interpreter and the translated code match Unicorn on all
  40, bit for bit.
- **Speed**, warm, one core, two runs, with the first build (blocks looked
  up by address; the current build, which chains blocks, measures 685 MIPS
  and 1.67x on the same 40):

  | Engine | Guest MIPS | Against Unicorn |
  | --- | --- | --- |
  | Unicorn JIT (live options, budget hook) | 397-409 | 1.00x |
  | cfcore translated | 706-718 | 1.73x-1.81x |
  | cfcore interpreter | 126 | 0.31x |

- **By stretch.** The aggregate is weighted by what was captured, not by the
  live mix, and the stretches differ:

  | Stretch starts at | Captures | Unicorn MIPS | Translated MIPS | Ratio |
  | --- | --- | --- | --- | --- |
  | 0x4005698c | 18 | 297 | 669 | 2.25x |
  | 0x40058c88 | 6 | 481 | 697 | 1.45x |
  | 0x40059dae | 16 | 441 | 730 | 1.65x |

- **Coverage.** Blocks came from the first 20 stretches only (507 blocks,
  3,872 instructions, 32 s to compile). On the other 20, 0.4% of the
  instructions ran in the interpreter. The other 20 are the same three
  stretches of the same factory pattern, so this shows the same paths are
  covered, not that unseen code is.

What this decides and what it does not:

- Block execution passes on x86-64, with a plain `match` over block
  addresses as the dispatcher and no chaining between blocks yet.
- Unicorn's live speed is close to its speed on the stretches: 108M
  instructions in about 0.32 s is 340 MIPS, against 397-409 measured alone,
  so about 15% of its engine time is outside straight block execution
  (engine entries, hook dispatch, the binding's memory and register calls).
  The replay below measures cfcore on the whole mix instead of estimating.
- The live mix is not the captured mix. Weighting the three ratios by each
  stretch's live share (30%, 24% and 14% of executed code) gives 1.7x. About
  18% of live code enters at points that were not captured (0x40059e2c,
  0x40059e3a, 0x40056a42).
- Not shown at all: arm64 speed and anything on an Apple device.
- The interpreter alone runs 126 MIPS on the desktop, about real time with
  nothing else running. It cannot be the main engine on either device.

## The whole CPU side, replayed (2026-10-07)

`native/cfcore/tools/record.py` records the real emulator thread while the
Cycles plays: every block Unicorn entered, the CPU state at the end of every
engine step, and everything the Python side did, in order: every hook, every
write to guest memory and registers, and every read of guest memory with the
bytes it got. `cfreplay` runs that in cfcore
([README](../native/cfcore/README.md)). Four recordings, one of 8,000 steps
(1.01 emulated s, 110M instructions) and three of 16,000 (2.03 s, 221M
each), 774M instructions in all:

- **Correct, with interrupts and devices.** Following Unicorn block by block
  with the interpreter, cfcore's registers, SR and EMAC state equal
  Unicorn's at the end of all 56,000 steps. A second of it crosses about
  10,400 exception entries, 69,000 device accesses, 8,300 code hooks and 110
  traps. Running each step again on its instruction count alone, with
  translated blocks, gives the same states.
- **Memory too.** Each of the 206,000 reads a second the Python side makes
  of guest memory (12.5 MB a second, the audio ring `Ssi0Dma` plays from
  among them) finds the same bytes in cfcore at that moment, and all 47 MB
  of guest memory is equal at the end of each recording, in both passes.
- **Exception entry is native.** cfcore's own entry (frame, A7, SR, PC from
  the vector table) equals what `Machine.raise_vector` wrote for all of
  them: 10,535 of 10,535, then 21,054, 21,056 and 21,049 of the same.
- **Speed on the full mix.** With blocks translated from an earlier
  recording (2,558 blocks, 12,967 instructions, 2.3 minutes to compile),
  the four take 0.216-0.223 s of CPU per emulated second, 490-500 MIPS,
  with under 0.02% of instructions interpreted. Unicorn's engine takes about
  0.32 s for the same work by the `perf` split above, so about 1.45x. The
  two figures are measured differently, and cfcore's includes the replay's
  bookkeeping and memory comparisons but no device work.
- **Coverage from a short sample.** Blocks from only the first 26 ms of
  another recording (1,710 blocks) left 0.3% of a full second to the
  interpreter. Every recording is the same factory pattern from the same
  snapshot, so this is one workload, not several.
- Chaining blocks by number instead of looking each up by address changed
  little (512 to 540 MIPS), so dispatch is not what limits it.

What it found:

- Unicorn's `mvz` takes N from the source byte or word, so a byte of 0x80
  or more sets N; the ColdFire manual says MVZ clears it. cfcore follows
  Unicorn, since that is the reference. It showed up as one differing SR in
  about 5,000 steps, when a step happened to end between the `mvz` and the
  next instruction that sets flags. [O] Which one the hardware does has not
  been checked here.
- The 0x4A3xxxxx-0x4A7xxxxx megabytes the firmware uses are separate memory
  from 0x423xxxxx in this machine (a byte written through one does not show
  in the other), not views of the same DDR. cfcore mirrors that. `Mem::alias`
  exists for the day they are.

Not in cfcore yet, and handed to the recording: `trap` (the RTOS yield,
about 110 a second), divide by zero, and any opcode the Cycles did not run.

On the A10 estimate above, 0.22 s here is about 0.44 s of the 0.77 s
budget, leaving 0.33 s for devices.

## Timers and forced interrupts in Rust (2026-10-08)

The first device models are ported: `native/cfcore/src/timers.rs` is
`emu/pit.py`'s `Pits` and `emu/dtim.py`'s `Dtims`, and `src/intfrc.rs` is
`emu/intfrc.py`'s `ForcedInterrupts` (both instances: INTC0's sequencer
chain and INTC1's panel-scan source). They reach the machine through a
small `Host` trait, so the same code will run on the real CPU and memory.

How they are checked: the recorder now also marks every `step` and
`service` call of the Python models with its instruction count and result.
In the replay the Rust model runs in the Python one's place at each of those
points, and everything it does is compared with what Python did there: the
step it returns, each register and memory write, each interrupt it raises
(frame, A7, SR, PC), and each register hook. Two recordings of 16,000 steps
(2.03 emulated s each):

- 63,996 `step` results and 63,996 `service` calls agree in each (four
  models, every engine step), and about 12,700 register hooks.
- The Rust models raise 13,913 and 13,911 of the 21,036 and 21,032
  interrupts themselves, the same ones at the same steps. The rest come
  from models that are still Python.
- The CPU states, host reads and final memory still match as before, in
  both passes.
- The check has teeth: a PIT clock rate off by one part in a million fails
  at step 15, and a forced-interrupt model that forgets to re-arm a cleared
  source fails at step 18.
- **Cost.** The replay takes 0.2255 s per emulated second with the Rust
  models running and checked, and 0.2235 s replaying Python's recorded
  effects instead: about 2 ms, inside run-to-run noise. The Python timers
  alone (`Timers.service` and `Pits.service`) were 29% of 0.85 s, about
  0.25 s.

Two things the port had to settle:

- Deadlines are f64 arithmetic in the same order as Python's, so they are
  the same bits. That only holds on the recording's clock, which counts
  Unicorn blocks times 3.87. A native machine will count cfcore's
  instructions, so its timers will be right by the same rules but will not
  line up step for step with a Python recording.
- Where Python walks a set of waiting sources (`all(render_holds(...) for
  ...)`), the order is CPython's hash order ({44, 57} gives 57 first) and
  the walk stops at the first no. The Rust models walk in source order, and
  the replay matches the render's answers by level. Order can matter in one
  way: a yes sets `m.render_waiting`, which makes the SSI end the step at
  the render's `rte`, so with mixed answers the flag depends on who was
  asked first. None of the 63,996 calls in either recording had mixed
  answers, so it is immaterial on this workload. [O] It has to be looked at
  again when the SSI is in Rust.

Not ported: the step loop itself (`longrun.spin`: the shortest step of all
sources, then services in order). It is a few lines, and it can only be
checked once every source it steps is in Rust.

**The clock unit (decided 2026-10-08).** The 64M "instructions" a second
the timers run on are Unicorn blocks times 3.87, plus the blocks the idle
skip credits. The recordings run 109M real instructions per emulated second,
about 1.7 for each counted one, and about half of emulated time is idle. A
native loop counts cfcore's real instructions, so its rate has to be set in
real instructions (counting them against 64M would give the firmware about
40% less CPU per emulated second), and its steps then stop lining up with
these recordings. Aileen's decision: accept that. Each model is checked
against recordings on the recording's clock, as here, and the assembled
native machine is judged by behaviour (it boots, plays the pattern, the
audio is right and in time). Recording Python in its exact `count=` mode on
a real-instruction clock, to get a step-for-step reference for the whole
machine, is kept in reserve for a misbehaviour that cannot be pinned down
otherwise.

## The audio path in Rust (2026-10-08)

`native/cfcore/src/ssi.rs` is `emu/ssi.py`'s `Ssi0Dma` for the Models'
profile (transmit channel 50, no receive): the request clock, the minor
loops in bulk or one at a time, the half and major interrupts on vector
170, the render forced on vector 191 from the handler's `rte`, and the
render window the timers ask about. Its clock is exact: the period is
64M/48000 instructions, not a whole number, so the next request is kept as
an integer over the request rate, where Python keeps a `Fraction`.

The audio check moved with it. The recorder now captures the samples the
Python model hands to the window (its `sink`), and the Rust model's output
is compared with them. One recording of 16,000 steps (2.03 emulated s):

- 79,995 `step` results and 79,995 `service` calls agree (five models), and
  27,941 hooks.
- All 779,008 bytes of audio the Python model handed on (2.03 s of 48 kHz,
  8 bytes a frame) come out of the Rust model the same, in the same
  pieces.
- The Rust models now raise 20,013 of the 21,059 interrupts. The rest are
  the idle loop's reschedule and `trap`.
- The Rust render window gives the same answer as Python's every time a
  timer asks about it, and no call had mixed answers, so the open point
  about the order of asking stays immaterial on this workload.
- The check has teeth here too: a half-boundary test off by one fails at
  step 7.
- **Cost.** With all five models running and checked the replay takes
  0.235 s per emulated second, against 0.227 s replaying Python's effects:
  about 8 ms.

## The eDMA copies and the panel in Rust (2026-10-08)

Two more models on the same harness:

- `src/edma.rs` is `emu/edma_sw.py`'s `SoftwareBank`: the channels the
  render starts by setting START (or through SSRT), their major loops and
  scatter-gather chains as copies, ring-buffer wrapping, and DONE shown when
  the firmware next reads the CSR.
- `src/board.rs` is `emu/modelboard.py`'s `ModelPanel` and PIT1 flag, with
  the ready line `emu/dsp.py`'s `Fifo` keeps on the same address: the scan
  columns, the encoders' quadrature phases, the pads' ADC samples, the LED
  rows, and input held for as many scan frames as the firmware needs.

The recorder now plays the panel while it records (two pads, a trig key, a
knob both ways) and marks each input call, and the replay gives the Rust
panel the same calls. One recording of 16,000 steps (2.02 emulated s):

- 95,994 `step` results and 95,994 `service` calls agree (six stepped
  models), and 126,487 hooks, among them every eDMA byte moved and every
  column, pad sample and ready word the firmware read.
- All 777,472 bytes of audio match, with the pads played.
- The eight panel inputs give the same scan bytes frame for frame.
- Broken on purpose: an eDMA that reloads CITER wrongly fails at step 2, and
  a panel with the encoder phases reversed fails at step 7,796, when the
  knob turns.
- **Cost.** With all seven models running and checked, 0.258 s per emulated
  second against 0.230 s replaying Python's effects: about 28 ms for the
  timers, forced interrupts, audio path, eDMA copies and panel together,
  comparisons included. Python spends about 0.52 s on the same.

The Rust models now raise 19,978 of the 21,014 interrupts in the recording.

## The rest of the per-step path, and the first native run (2026-10-08)

Ported and checked on the same harness, on one recording of 24,000 steps
(3.04 emulated s, with the panel played):

- `trap` in cfcore itself: all 344 match the entries Python made.
- `src/rtos.rs`: the idle-loop hook (`do_halt`: the rest of the step
  credited, the reschedule vector every 20,000 passes) and the semaphore
  stand-in (`satisfy` and `never_fake_set_posts`).
- `src/board.rs`: the I2C bus and codec register file (`emu/i2c.py`).

With those, every one of the recording's 31,501 exception entries is made
by Rust: 31,157 by the models and 344 traps. 143,994 `step` results,
143,994 services and 203,735 hooks agree, and all 1,165,568 bytes of audio.

**The whole machine, natively.** `src/machine.rs` is the step loop
(`longrun.spin`) over the core and all the models, and `cfrun` runs it from
a recording's starting state with nothing replayed but the panel input.
Its clock counts real instructions: 211.7M a second, the rate the recording
ran at while not idle (it was idle 48% of the time), with the Python
state's deadlines rescaled to it. On the Ryzen 5 5600G, x86-64:

| | Python emulator | Native (cfrun) |
| --- | --- | --- |
| CPU per emulated second | 0.85 s | 0.23-0.24 s |
| Times real time | 1.17x | 4.2x |

- **It plays the same thing.** Over the recording's 3.04 s the native run
  plays the same 145,696 frames' worth; the first 32,577 (0.68 s) are
  bit-identical to Python's, and after that the two drift by a couple of
  samples: correlation 0.992 at a lag of 2 samples, level 9,118 against
  9,099, and the level per 0.1 s tracks throughout, the pad hits included.
  It takes 31,158 interrupts from the models where Python's took 31,157.
- **It keeps going.** 120 emulated seconds run in 27.5 s of CPU (0.229 s per
  emulated second) without stopping, 5,760,000 frames exactly, the level
  per second between 7,486 and 12,056 from start to end.
- 0.06% of instructions are interpreted, with 4,072 blocks translated from
  that recording (19,424 instructions; they take 7 minutes to compile).

On the A10 estimate above (half this desktop), 0.23 s is about 0.47 s per
emulated second: about 2.1x real time against the 1.3x target. That is
still an estimate. Nothing has run on arm64.

What this does not cover:

- **One workload.** The factory pattern from one snapshot, with a few panel
  inputs. Code the recordings never ran is interpreted, or not implemented
  at all in cfcore (then the run stops and says where).
- **Not from power-on.** It starts from a state Python saved. Boot, the
  +Drive (`esdhc`), MIDI, the soft-float and screen shortcuts, and the
  display are not in the native machine. The screen is in guest memory but
  nothing reads it out yet.
- **Judged by sound, as decided.** After the first 0.68 s the native run is
  not Python's run: its clock is a different unit. The time constants the
  models carry (`PENDING_STEP`, `RENDER_MAX`, `ARM_STEP`) are still the
  Python clock's numbers, used unscaled.
- Nobody has listened to it. `cfrun --wav DIR` writes `native.wav` and
  `python.wav` for that; the first runs' are in `out/cfcore/run1/` and
  `out/cfcore/run120/`.
- The "broken on purpose" tests cover the timers, the forced interrupts,
  the audio path, the eDMA and the panel. The idle and semaphore stand-ins,
  `trap` and the I2C bus were not tested that way, and the I2C bus saw only
  a handful of accesses in the recording, so its check is thin.
- The 4.2x is the emulation alone. On a device the touch panel and the
  audio output add to it.

## The native machine on arm64 (2026-10-07, M1 MacBook Air)

[C] The sections above say nothing has run on arm64. `cfrun` now has, on
the build Mac: an M1 MacBook Air (8 GB, macOS 27.0, rustc 1.99.0,
`aarch64-apple-darwin`), on AC power. Not on an iPhone or iPad: this is the
first arm64 figure, not the plan's gate.

The inputs were copied from the desktop's `out/cfcore/`:
`cycles-play.trace` and `aot_gen.rs` (4,072 blocks). The build with the
blocks took 6 min 12 s.

- **The same run as on x86-64.** Over the recording's 3.04 s: 145,696
  frames, the first 32,577 bit-identical to Python's, then correlation
  0.9923 at a lag of 2 samples, level 9,118 against 9,099, 31,158
  interrupts from the models, 0.06% of instructions interpreted. Every one
  of those figures equals the Ryzen's.
- **Speed.** 120 emulated seconds, two runs: 0.1825 and 0.1812 s per
  emulated second, 5.5x real time (the Ryzen: 0.23 s, 4.2x). 13,133M
  instructions in 21.8 s is about 600 MIPS on the whole mix. A third run
  under the sampler took 0.1875 s.
- **`cfrun`'s "s of CPU" is wall time** (`Instant`). On an idle machine the
  two agree (22.7 s user for 21.8 s wall); they do not when the process is
  descheduled.
- **Efficiency cores, as a floor only.** Under `taskpolicy -b` (background
  priority, which also clamps the efficiency cores' clock), 60 emulated
  seconds took 58 s of user CPU, 62 and 89 s of wall: about 1.0x. This is
  not an A10 estimate. It shows there is no margin on a core that slow.
- **Memory.** Peak footprint 1.0-1.1 GB, nearly all of it the 430 MB trace
  being read and parsed; 196 MB while running. A device benchmark must not
  load this trace on the 2 GB iPad: it needs a small starting-state file.

Where the time goes, from `sample cfrun 12 1` during the 120 s run (9,015
samples, by top of stack):

| Share | Where |
| --- | --- |
| 84% | `aot::run`: the translated blocks, their handlers inlined |
| 9% | eDMA copies: `Bank::transfer` 4.9%, `Mem::write_bytes` 2.9%, `Bank::mapped` 1.1% |
| 3% | the allocator (`malloc`, `realloc`, `free`) |
| 1% | `main` (the step loop, inlined) and `DevBus::access` |
| 0.7% | `Interp::step` |

- The devices are not the cost any more: everything outside translated
  code is about 16%.
- Two cheap things in that 16%: `edma.rs` and `ssi.rs` allocate a buffer
  for each transfer (`span`'s `vec!` and the one in the TCD copy in
  `edma.rs`, `captured` in `ssi.rs`), and the copies go through
  `write_bytes` a range at a time. Reused buffers and a direct copy could
  take back most of the 12%. Not done.
- [O] Which blocks or handlers dominate inside `aot::run` on arm64. The
  sampler sees one function. A build with debug info would map samples to
  guest addresses.

For the device runs: the library type-checks for `aarch64-apple-ios`
(`cargo check --lib --target aarch64-apple-ios`), but this Mac has only the
Command Line Tools, so there is no iOS SDK to link against. Full Xcode is
needed, and the disk had 14 GB free.

## A small starting-state file, and cheaper copies (2026-10-07, desktop)

After the arm64 run, on the Ryzen:

- **`cfstart TRACE OUT`** cuts a recording down to what `cfrun` uses: the
  starting state (the file's own bytes, copied), the panel input with its
  times, and the audio Python played. For the 430 MB recording it is 50 MB
  (`out/cfcore/cycles-play.start`), nearly all of it the 47 MB of guest
  memory. `cfrun` on it peaks at 100 MB of memory, where the Mac saw
  1.0-1.1 GB with the whole recording.
- A run from the small file plays what a run from the whole recording
  plays: `native.wav` is byte-identical over the 3.04 s and over 120 s.
- **Copies.** `Mem::read_bytes` and `write_bytes` now copy a megabyte run
  at a time (they looked up every byte), and the eDMA mapped check no
  longer builds a list of pages. `cfreplay` agrees as before in both passes
  (states, host reads, final memory, 31,501 exception entries, models,
  audio), and both WAV files equal the ones the earlier build wrote. 120
  emulated seconds: 0.219 s per emulated second here, from 0.229. Not yet
  timed on arm64.
- Not done: the buffers `edma.rs` and `ssi.rs` still allocate per transfer.
- `cfrun` now says "wall time" for what it measures.

## On the iPhone 13 (2026-10-07)

The first run on an Apple device: the whole native machine in an app on the
iPhone 13 (iPhone14,5, A15), which runs iOS 26.6.2, not the 27.0.1 this
plan assumed. Built on the M1 MacBook Air with Xcode 27.0 and signed with a
free Personal Team. Not yet on the A1954 iPad, which is the gate.

What was built:

- `native/cfcore/src/ffi.rs`: C entry points that open `cfstart`'s file and
  run the machine a chunk at a time. Each chunk reports its instructions,
  interrupts, frames and an FNV-1a hash of the audio it played, then drops
  the audio, so a long run does not grow.
- `cfchunks`: the same chunks on the Mac, for the hashes to compare with.
- `native/ios/`: CyclesBench, a small SwiftUI app around those entry points
  (`build.sh` builds the library for `aarch64-apple-ios`, the default
  baseline CPU, and generates the project with xcodegen). It runs chunks of
  10 emulated seconds on a thread of its own and logs each one's wall and
  thread CPU time, the thermal state and the memory footprint, on screen and
  to `Documents/cfbench.log`. Deployment target iOS 17.0, for the iPad.

The Mac first, with the desktop's cheaper copies: `cfrun` on
`cycles-play.start` takes 0.1665 and 0.1647 s per emulated second over 120
s (6.0x, from 0.182), and peaks at 161 MB. The 3.04 s check is unchanged
(32,577 identical frames, 31,158 interrupts).

The phone, 600 emulated seconds in 60 chunks, the screen on, on USB power:

| | iPhone 13 | M1 MacBook Air (`cfchunks`) |
| --- | --- | --- |
| Wall per emulated second, mean | 0.1728 s (5.79x) | 0.1738 s (5.75x) |
| Slowest chunk | 0.1950 s (5.13x) | 0.2110 s |
| First chunk | 0.1529 s (6.54x) | |
| Chunk 30 | 0.1682 s (5.95x) | |
| Chunk 60 | 0.1907 s (5.24x) | |

- **The same audio.** All 60 chunks' hashes equal the Mac's, so the 600 s
  of audio (28.8M frames) are the same bytes, and each chunk's instruction,
  interpreted and interrupt counts are equal too.
- **It slows as it warms**, by about 25% over the 100 s of wall time the
  run took: 0.153 at the start, 0.168 halfway, 0.19 at the end. The thermal
  state read "fair" throughout, from the first chunk. Unpaced, this is one
  core flat out, which a paced app would not do, so it is the worst case.
  [O] Where it settles: 30 minutes was not run.
- **CPU time equals wall time** (0.1728 both), so the thread was not
  descheduled or moved to a slow core.
- **Memory**: 57 MB after opening, 64 MB at the end.
- **Launch**: started from the Mac with `devicectl device process launch`
  and no debugger. [O] Launched by hand with the Mac disconnected, in
  airplane mode, and after a restart: not done.

A first 120 s run had 11 of 12 hashes equal. The twelfth differed because
the app asked for a slightly shorter last chunk (32 frames fewer), not
because the machine did: the app now runs whole chunks, and the 60 above
are from that build.

## On the iPad, and playing it (2026-10-07)

**The gate.** The same CyclesBench build on the A1954 iPad (iPad7,6, A10
Fusion, iPadOS 17.7.11), started from the Mac with no debugger, on USB
power, the screen on. It was asked for 600 emulated seconds; 52 chunks ran
before another app was launched over it, which suspended it.

| | iPad (A10) | Target |
| --- | --- | --- |
| Chunks 7 to 51, wall per emulated second | 0.293-0.332 s, mean 0.298 (3.4x) | 1.3x |
| Chunks 1 to 6 | 0.373-0.409 s (2.4x-2.7x) | |
| Chunks 1 to 51, mean | 0.309 s (3.2x) | |

- **The same audio.** All 52 chunks' hashes equal the Mac's, and the
  instruction, interpreted and interrupt counts with them.
- **No throttling seen**: the thermal state read "nominal" in every chunk,
  and the run got faster after the first minute, not slower. [O] Why the
  first six chunks are slower was not looked into (the app had just been
  installed and launched).
- **Memory**: 55 MB after opening, 62 MB at the end.
- The plan's estimate for the A10 was half the desktop, about 2.1x. It is
  3.4x: 0.30 s against the Ryzen's 0.22-0.23 s.
- Chunk 52 is not in the figures: its wall time (0.444) is over its CPU
  time (0.358) because the app was being put in the background.
- [O] The whole 600 s, 30 minutes, and a launch by hand with the Mac
  disconnected: not done on either device.

**Playing it.** A second app in `native/ios/`, Cycles, plays the machine on
both devices: sound, the panel, the screen and the LEDs. It starts from the
same bundled state (the factory pattern playing) with the recording's panel
input dropped, and keeps nothing when it closes.

- **Sound.** One thread runs the machine in slices of 128 frames whenever
  the output has less than 16 ms waiting, into a ring with no lock that an
  `AVAudioSourceNode` reads. The session asks for 48 kHz and a 5 ms buffer.
  The output follows the codec level the firmware's VOLUME sets, as
  `emu/modelboard.py`'s `output_gain` does.
- **The panel** is `emu/mdpanel.py`'s layout over the drawn faceplate in
  `emu/assets/panels/model-cycles` (plate, caps, light masks), with the
  wiring from `devices/model-cycles.toml`. Each finger is its own control,
  so a key or trig can be held while a knob turns. A knob turns by dragging
  (6 points a detent, as the window); a tap on PITCH is its push switch; a
  pad's velocity is where it is touched.
- **The screen** is read from the frame buffer the pointer at 0x401492f0
  names, up to 30 times a second (`cfscreen` prints the same read on the
  Mac). It is read whenever the timer says, not at the firmware's swap, so
  a frame can be torn. The second address `emu/symbols.py` finds near it
  (0x4014553c) does not hold a buffer pointer in this state.
- Aileen played it on the iPhone 13 and reports that it looks and works
  well, sound included. It is installed and running on the iPad; nobody has
  reported on it there yet.
- Not measured: latency from touch to sound, underruns, and the CPU the
  app takes while playing.
- Not there: saving, the +Drive, MIDI, background audio, starting from
  power-on, and a layout for the phone (it is the window's, scaled down).
- Where it differs from the window: every knob shows its pointer from the
  start (the window shows one once a knob has turned), dark on the three
  pale knobs; PITCH cannot be held down while it turns; the four page
  lights by DELAY TIME are the plate's dark ones (the device file names no
  LEDs for them, so the window does not light them either).

## What the Python side did per second (the scope that was ported)

Counted before the port, for the playing Cycles, per emulated second at
100% of real time (every Unicorn hook, and the event sources
`longrun.spin` steps). All of it is now in Rust except `midi.MidiIn`,
which had nothing to do:

| What | Per second | Where |
| --- | --- | --- |
| Engine entries (a timer or event was due) | 7,906 | `longrun._FastStepper.run`, from 196 `spin` chunks |
| Timers stepped at each entry | | `pit.Pits`, `dtim.Dtims` under `dtim.Timers` |
| Other event sources stepped | | `ssi.Ssi0Dma`, `edma_sw.SoftwareBank`, `intfrc.ForcedInterrupts`, `midi.MidiIn` |
| Panel scan device, writes and reads at 0x8C000002 | 4,884 and 3,907 | `modelboard.ModelPanel` (and a `dsp.Fifo` hook on the same address) |
| Pad ADC reads, 0xFC094012 | 3,907 | `modelboard.ModelPanel` |
| Idle loop hook, 0x400057e6 | 4,506 | `longrun.build` `do_halt` (fast idle) |
| Forced-interrupt register writes, INTC1 and INTC0 | 3,978 and 768 | `intfrc.ForcedInterrupts` |
| SSI0 audio: forced interrupt, its return, the eDMA interrupt clear | 3,001, 3,001 and 1,501 | `ssi.Ssi0Dma` |
| DTIM register reads and writes | 1,501 and 29 | `dtim.Dtims` |
| Task switch hook, 0x4000044a | 564 | `longrun.build` `switch_to` |
| Exceptions taken through the Python hook | 113 | `harness.Machine.install_exceptions` |
| Semaphore posts and satisfied waits | about 190 | `longrun.never_fake_set_posts`, `longrun.build` `satisfy` |
| I2C codec | 3 | `i2c.I2cBus` |

Installed but not called while it played: soft-float and setPixel shortcuts
(`softfloat`, `hle`), the SD card (`esdhc`, `gpio.SdGate`), the hardware
eDMA request paths, the MOVEC patches, and the fault hook. Native in the
Unicorn library already, so not in the counts: the block budget, the
software eDMA transfers, FF1 and `rte`.

Next (the gate is passed: 5.8x on the phone, 3.4x on the iPad, above):
what a session needs beyond playing (saving, the +Drive, starting without
a Python-made state).

## What can be reused

- `devices/model-cycles.toml`: firmware identity, key/pad/encoder/LED mapping,
  128 MB guest DDR configuration, and 48 kHz audio configuration.
- `emu/modelboard.py`, `emu/i2c.py`: scanned panel, velocity pads and codec.
- `emu/harness.py`, `emu/longrun.py`, timers, SSI/eDMA and storage models:
  the existing machine and execution machinery.
- `emu/session.py`: deterministic headless sessions for comparison tests.
- `emu/gui.py:Emulator`: live scheduling, input, audio and screen publication,
  after extraction from the Tkinter module.
- `emu/bootstrap.py`, `emu/release.py`, snapshot/checkpoint and +Drive code:
  firmware preparation and persistence.
- `emu/mdpanel.py`, `emu/panellayout.py` and panel assets: visual reference
  and layout data, with a new native touch implementation.

Model:Cycles is a useful narrow target: it does not require the Digitone's
second emulated processor, and its factory sounds are in its firmware.
See [MODELS.md](../MODELS.md). Desktop operation is documented; iPad speed
is not measured. Existing desktop benchmarks are not iPad capacity estimates.

## 1. Prove an AOT ColdFire backend before porting the interface

Treat the A10 as the performance target from the beginning. Compile for its
supported arm64 instruction set, not the M1 build host's native CPU flags.
Run the first device smoke test on the A15, but keep the shared baseline
binary A10-compatible; gate any later CPU-specific optimization explicitly.
Set a deployment target compatible with the installed OS: this iPad is
absent from Apple's [iPadOS 18 compatibility list](https://support.apple.com/en-sg/104986),
so do not require iPadOS 18+ frameworks or APIs. Use its wired headphone
output for initial audio/latency tests. Keep screen publication bounded
(initially 30 Hz), avoid unbounded PCM capture and duplicate guest-memory
snapshots, and measure process memory on the real device. Move Python hot
paths to native code only where profiling establishes a need. M1 benchmark
results cannot establish A10 viability.

Pin Model:Cycles OS 1.13 by its SHA-256 in `devices/model-cycles.toml`.
Use the existing emulator to collect baseline CPU states, LCD frames and
PCM for boot, all six machines, patterns, effects, editing and saving.
Profile the synthesis workload and identify executed code ranges, indirect
branch targets and writes to executable guest memory. Traces guide coverage;
they are not proof that all possible execution paths were discovered.

Build a small backend prototype on the Mac first:

1. Define explicit ColdFire state: registers, PC/SR/CCR, EMAC accumulators
   and control registers, guest memory, interrupt state and execution budget.
2. Choose a translation approach after inspecting the pinned QEMU/Unicorn
   internals: either emit C/C++ or LLVM IR from decoded ColdFire instructions,
   or reuse its translator with a new static-code emitter. Neither is a
   ready-made build switch. Reuse the repository's semantic tests and fixes.
3. Compile a limited set of hot basic blocks with Clang, using a dispatcher
   keyed by guest PC and a version/hash of the corresponding guest code.
   Resolve indirect branches through this dispatcher rather than assuming
   all destinations were discovered statically.
4. Provide a no-JIT ColdFire interpreter for uncovered or modified blocks.
   Audit candidate cores for ColdFire ISA_C and EMAC support; generic 68000
   support is insufficient. QEMU/TCG interpreter integration is another
   candidate to investigate, not an assumed feature of pinned Unicorn.
5. Preserve memory/MMIO hooks, byte order, exception frames, RTE, condition
   flags and interrupt delivery. End execution at the same supported timer
   boundaries as the reference, including a way to stop within a compiled
   block when required. Preserve existing high-level accelerators.
6. Detect guest code writes (including DMA), invalidate affected compiled
   mappings and use the interpreter until a matching precompiled version
   exists. Runtime fallback must never silently call Unicorn's JIT.
7. Differentially test registers, memory, exceptions and audio against the
   patched desktop engine. Include cold paths and forced interpreter fallback.

First prove a representative sound-rendering workload plus surrounding
scheduler execution. Expand static coverage based on time spent in fallback,
then test boot and the full six-track workload. The target is the complete
Cycles machine, not just a substitute synth with similar sounds.

Cross-compile the resulting native backend into a minimal iPhoneOS arm64
Xcode app. Test launch with no debugger, airplane mode, device reboot and
force-quit/relaunch. Measure fallback cost and sustained rendering throughput
on the actual iPad. Keep any generated source and object cache in an ignored
private build directory; record firmware hash, generator version and flags.

**Exit condition:** a correctness-checked AOT/interpreter backend runs a
representative Cycles audio workload on the iPad without JIT and with
credible real-time headroom. If this fails, investigate the measured backend
bottleneck before building the full app. This is the largest unknown.

## 2. Embed a minimal headless runtime

Use an Xcode app with embedded CPython and the existing Python machine
models, connected through a small native bridge to the new AOT/interpreter
backend. Audit the Unicorn calls and custom native exports used by the
machine, then implement a compatibility adapter or a shared CPU interface. Python's supported iOS
integration is [embedding inside an app](https://docs.python.org/3.14/using/ios.html).
Avoid a wholesale C++ rewrite before measurements show it is necessary.

The repository pins Python 3.12 and imports `audioop`. Select a supported
iOS CPython toolchain, then resolve the version change explicitly: replace
or isolate `audioop` before moving to Python 3.13+, and verify snapshot and
numeric compatibility. Audit native dependencies and bundle only those
needed by the Cycles boot/runtime path; determine whether Capstone or
pypcode is actually needed before attempting their iOS builds.

Extract `Emulator` from `emu/gui.py` into a module that imports no Tkinter.
Keep the desktop UI as a client of it. Inject platform services for audio,
MIDI, paths and lifecycle. Replace launcher subprocesses with calls on an
owned worker thread; retain a single owner for machine state and deliver
controls through queues. Adapt ctypes/library lookup to bundled, signed
iOS frameworks, including handling `sys.platform == 'ios'`.

Proposed boundary: start/stop, key down/up, pad velocity, encoder delta,
latest LCD/LED frame, audio blocks, save/restore, and status/error reporting.

For the first test, prepare a private firmware/snapshot/+Drive bundle on
the Mac and import it. Verify host-independent snapshot restoration before
relying on that shortcut. Then add on-device first boot. If `.syx` import is exposed, accept only
the firmware hash matching the compiled backend; other versions require
a new Mac build.
Use only the user's firmware and keep generated firmware data out of git.

**Exit condition:** boot or resume Model:Cycles on iPad and produce matching
LCD frames and PCM for a fixed input script against the desktop baseline.

## 3. Establish real-time audio before building the full panel

Connect emulation output to a bounded native PCM ring buffer. A native
AVAudioEngine/AVAudioSourceNode or Audio Unit callback drains it; it must
not enter Python, run the emulator, write files or wait on locks. Run the
emulator on its own thread and pace it by audio demand with bounded lead.

Keep the firmware at 48 kHz. Request that device rate, inspect the actual
route rate and convert at the output boundary when necessary. Apple notes
that [preferred sample rate is a preference, not the actual rate](https://developer.apple.com/documentation/avfaudio/avaudiosession/preferredsamplerate).
Handle interruptions, route changes and headphone/USB disconnection.

Create a Cycles-specific benchmark: `tools/capbench.py` currently scripts
Digitakt-style sample/track controls, so its default workload is unsuitable.
Exercise all six Cycles machines/tracks, dense patterns, long decays, delay,
reverb, automation and simultaneous input. Measure unpaced capacity, audio
underruns, worst delivery gaps, memory, touch-to-sound latency and thermal
behavior over at least 30 minutes.

**Proposed pass target:** at least 1.3x sustained unpaced capacity on the
target iPad, no underruns in the paced stress test, and measured playable
latency (aim for under 30 ms on a wired output). These are acceptance targets,
not current capabilities. Average throughput alone is insufficient.

## 4. Implement the native touch panel

Use SwiftUI for app navigation and UIKit touch handling where it gives
better simultaneous gesture control. Render the existing 128x64 LCD as a
nearest-neighbor texture and publish LED changes without blocking audio.

Include all 16 encoders, 16 trigs, six pads, transport/function keys and
PITCH push. Preserve VOLUME's firmware/codec behavior. Support holding FUNC
or a trig while turning a knob, multiple simultaneous pads, encoder fine
adjustment and explicit release on touch cancellation. Use touch position
or a chosen fixed value for pad velocity; do not assume pressure-sensitive
touch hardware. Adapt the layout to the actual iPad's safe areas and size.
For iPhone, add a landscape layout with switchable control pages and a
persistent transport/track strip; keep controls large enough for multitouch
parameter locks rather than shrinking the entire tablet panel.

**Exit condition:** create a pattern, set parameter locks, change machines,
adjust effects and save a project using only the iPad's touch interface.

## 5. Finish storage, lifecycle and installation

Bundle the matching firmware in the private personal build, or import that
exact `.syx` through Files and copy it into app-owned storage. Validate its
hash against the compiled backend before running it. Keep +Drive,
snapshots and extracted data in Application Support. Audit sparse image
size and actual disk use; do not assume copying preserves sparsity.

Provide explicit Save plus periodic safe checkpoints. Save at coordinated
emulation boundaries and replace files atomically. An iPad app cannot rely
on a desktop window-close handler: test backgrounding, suspension, memory
pressure and forced termination, and explain recovery to the last checkpoint.
Start with foreground playback; add background audio only with a tested
audio-session/lifecycle implementation.

Add CoreMIDI input/output after touch and audio are stable, mapping host
events into the existing emulated DIN path. Host USB MIDI does not require
emulating the Model:Cycles USB controller. Defer AUv3 to a separate project
phase because extension lifecycle, memory and JIT constraints need their
own feasibility work.

Build/install through Xcode on the Mac. A free Personal Team's provisioning
profiles [expire after seven days](https://developer.apple.com/help/account/basics/about-your-developer-account/).
Independent launch removes JIT activation; it does not remove signing expiry.
Document the chosen signing and refresh procedure. App Store/TestFlight
distribution is outside the initial personal development build.

**Exit condition:** airplane-mode performance with the Mac disconnected,
reliable project/session recovery, and a reproducible install/launch guide
that needs no debugger or JIT activation at launch while signing is valid.

## Suggested implementation order and decision points

1. Desktop reference corpus and AOT/interpreter backend design.
2. Minimal compiled Cycles audio workload with differential correctness tests.
3. iPhone 13 independent-launch/audio proof, followed immediately by the
   A1954 iPad performance gate using the same workload.
4. Headless runtime extraction, embedded Python and complete boot/resume.
5. Native audio bridge and six-track performance/latency report.
6. Touch panel, persistence, private firmware build pipeline and install guide.
7. CoreMIDI and optional background playback.

Keep the existing desktop backend working throughout. The iOS simulator is
useful for UI development but cannot establish physical-device performance
or independent-launch behavior.

Do not assign a firm full-port timeline before the backend investigation:
this includes compiler/emulator work, not just an iOS interface. Establish a
small working slice first, then estimate remaining instruction coverage,
integration and optimization. No implementation or physical-device testing
was performed as part of preparing this plan.
