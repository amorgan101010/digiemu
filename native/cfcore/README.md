# cfcore

A ColdFire V4e/EMAC core in Rust with no runtime code generation, for the
Model:Cycles iPad/iPhone port ([the plan](../../docs/MODEL-CYCLES-IPAD-PLAN.md)).
iOS does not let an app create executable memory, so the port cannot use
Unicorn's JIT. This core has two ways to run guest code:

- an interpreter (`src/interp.rs`);
- blocks translated to Rust ahead of time from one firmware image
  (`cfgen`), compiled into the binary, with the interpreter for anything
  not translated.

Both call the same instruction handlers (`src/ops.rs`): a translated block
is those handlers called with constant operands. `src/insn.rs` has the one
table that names every instruction, from which the interpreter's dispatch
and the generator's output both come.

What it has:

- the CPU with the instructions the playing Model:Cycles uses, exception
  entry, `trap` and `rte`, RAM, device memory behind a `Bus` callback
  (`src/cpu.rs`), and an exact instruction budget;
- the device models on the playing Cycles' per-step path, each a port of
  the Python one: the PIT and DMA timers (`src/timers.rs`, from `emu/pit.py`
  and `emu/dtim.py`), software-forced interrupts (`src/intfrc.rs`), the
  audio output path (`src/ssi.rs`), the software eDMA channels
  (`src/edma.rs`), the front panel, PIT1 flag and I2C codec
  (`src/board.rs`, from `emu/modelboard.py`, `emu/dsp.py` and `emu/i2c.py`),
  and the idle-loop and semaphore stand-ins (`src/rtos.rs`, from
  `emu/longrun.py`);
- the loop that steps them all (`src/machine.rs`, `longrun.spin`), and
  `cfrun`, which runs the whole machine from a recording's starting state
  with no Python in it.

What it does not have: boot, the +Drive, MIDI, the display read-out, and any
opcode the Cycles did not run in the recordings (the run stops and says
where). It starts from a state the Python emulator saved.

Translated code, workload files and traces are firmware-derived. They are
written under `out/` or outside the tree, never committed.

## The reference

Everything is checked against digiemu's patched Unicorn. The tools need one
that can read and write the EMAC registers: the six patches in `patches/`,
then `tools/unicorn-mac-accessor.patch`, built as
`tools/install-patched-unicorn.sh` builds it but into a directory of its
own. Name that directory in `LIBUNICORN_PATH` for the Python tools here.

They run from a copy of the firmware folder and make no sound:

```sh
cd COPY_OF/portable/firmware/mc-1.13-44fe5862
export DT2_DEVICES=REPO/devices DT2_PLUSDRIVE='' DIGIEMU_MUTED=1
```

## Replaying a recorded run (the whole machine's CPU side)

`tools/record.py` records the real emulator thread while the Cycles plays:
the machine at the start, every block Unicorn entered, the CPU state at the
end of every engine step, and everything done from outside, in order: each
hook that fired, each write the Python side made to guest memory and
registers (interrupt entries are such writes).

```sh
python REPO/native/cfcore/tools/record.py \
  snapshots/model-cycles_OS1.13/gui.snap RUN.trace 8000     # 8000 steps, about 1 s
cargo build --release
target/release/cfreplay RUN.trace --seeds RUN.seeds        # interpreter only
target/release/cfgen --image sections/section_3_MAIN_OS.bin \
  --seeds RUN.seeds --out REPO/out/cfcore/aot_gen.rs
CFCORE_AOT=REPO/out/cfcore/aot_gen.rs cargo build --release
target/release/cfreplay RUN.trace
```

`cfreplay` runs the trace twice. Pass 1 follows Unicorn block by block with
the interpreter, feeds every recorded hook and host write at its place, and
compares every register, the status register and the EMAC state at the end
of every step; it stops at the first difference. It also checks that this
core's own exception entry gives the frame, stack pointer, SR and PC that
the Python side wrote for each interrupt, that every read the Python
side made of guest memory finds the same bytes here, and that all of memory
is equal at the end. Pass 2 runs each step on its instruction count alone,
with the translated blocks, makes the same checks and is timed.

The recorder also marks each `step` and `service` call of the Python
models that have a Rust port, and captures the samples the audio model
hands on. At those points the replay runs the Rust model instead and
compares what it does with what Python did: the step it returns, every
write, every interrupt it raises, every hook, every sample. Set
`CFREPLAY_NO_MODELS=1` to replay Python's recorded effects instead, which
tells a CPU difference from a model difference. A new model goes on the
same harness: mark its calls in `tools/record.py`, give it a source number,
and run it from `Stream::timers` and the bus in `src/bin/cfreplay.rs`.

Do not pipe `cfgen` through `head`: it stops at the closed pipe before it
writes its output.

## Running the whole machine

On this machine the pieces are kept, uncommitted, in `out/cfcore/`:
`cycles-play.trace` (a 3 s recording of the playing Cycles with the panel
played, 430 MB), `cycles-play.seeds` and `aot_gen.rs` (its blocks),
`unicorn-accessor/` (the Unicorn the recorder needs, for
`LIBUNICORN_PATH`), and `run1/` and `run120/` (the WAV files of the first
native runs).

```sh
target/release/cfreplay RUN.trace     # prints the recording's real instruction rate
target/release/cfrun RUN.trace --ips 211700000 [--seconds 120] [--wav DIR]
```

`cfrun` takes only the starting state and the panel input from the
recording. It prints the CPU time per emulated second, and compares the
audio it plays with the recording's: how long the two are bit-identical,
then level and correlation, since its clock counts real instructions and
the two runs drift apart by samples. `--wav` writes both as WAV files (to
somewhere ignored: they are the firmware's output).

## Stretches without devices

A workload file is a stretch of the render between two device accesses:
entry state, the pages it touches, and Unicorn's final state, memory
included. This is the like-for-like speed comparison with Unicorn's JIT.

```sh
python REPO/native/cfcore/tools/capture.py \
  snapshots/model-cycles_OS1.13/gui.snap WORKLOADS 40
target/release/cfbench WORKLOADS/w*.bin > rust.jsonl
python tools/uctime.py WORKLOADS/w*.bin > unicorn.jsonl
python tools/compare.py rust.jsonl unicorn.jsonl
```

`cfgen` also takes workload files as block sources. `tools/capture.py` has
the three addresses it starts captures from; they are Model:Cycles OS
1.13's.
