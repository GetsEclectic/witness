// witness-audiotap: macOS mic + system-audio capture for witness.
//
// Builds a private CoreAudio aggregate device combining:
//   * the user's default input device (mic) as a sub-device
//   * a system-audio process tap (excluding our own PID) as a sub-tap
// and writes interleaved Float32 PCM to stdout at a FIXED rate (--rate,
// default 48 kHz), 2 channels:
//   * channel 0 = mic (downmixed to mono if the device is multi-channel)
//   * channel 1 = system audio (downmixed to mono from the stereo tap)
//
// The output rate is a contract, not a report. The aggregate itself may run at
// whatever rate its sub-components agree on (a Bluetooth headset in HFP mode
// forces 16 kHz; a 44.1 kHz output device forces 44.1 kHz), and that rate can
// CHANGE MID-CAPTURE when the user's devices change. We resample to the fixed
// output rate so the parent can hand ffmpeg one `-ar` for the life of the
// process and the stdout byte stream stays continuous across a device swap.
//
// Why a single binary rather than ffmpeg avfoundation + a separate tap:
// ffmpeg 7's avfoundation demuxer doesn't unblock from its sample-buffer
// queue on SIGINT, so when the tap pipe closes ffmpeg hangs and gets
// SIGKILLed without writing the opus trailer (zero-byte file). Doing all
// capture in this binary means ffmpeg has only one pipe input that we can
// close cleanly to drive shutdown.
//
// macOS 14.2+ (CATapDescription / AudioHardwareCreateProcessTap).

import Foundation
import CoreAudio
import AudioToolbox

// MARK: - Args

var outRate: Double = 48000
var probeMicRunning = false

do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = it.next() {
        switch arg {
        case "--rate":
            guard let v = it.next(), let d = Double(v) else { fatalError("--rate needs a number") }
            outRate = d
        case "--channels":
            // Reserved for back-compat with older Python platform module; the
            // output is always 2 channels (mic, system).
            _ = it.next()
        case "--mode":
            // Reserved for future PID-targeted taps. Only "system+mic" today.
            _ = it.next()
        case "--probe-mic-running":
            probeMicRunning = true
        case "--help", "-h":
            print("usage: witness-audiotap [--rate 48000]")
            print("       witness-audiotap --probe-mic-running")
            print("Captures default mic (ch0) + system audio excluding self (ch1) via a")
            print("CoreAudio aggregate device and writes interleaved Float32 PCM to stdout")
            print("at a fixed sample rate, resampling if the device runs at another rate.")
            print("macOS 14.2+.")
            exit(0)
        default:
            FileHandle.standardError.write(Data("witness-audiotap: unknown arg \(arg)\n".utf8))
            exit(2)
        }
    }
}

// MARK: - Helpers

func note(_ msg: String) {
    FileHandle.standardError.write(Data("witness-audiotap: \(msg)\n".utf8))
}

func die(_ msg: String, status: OSStatus = 0) -> Never {
    var s = "witness-audiotap: \(msg)"
    if status != 0 { s += " (OSStatus=\(status))" }
    FileHandle.standardError.write(Data((s + "\n").utf8))
    exit(1)
}

func getAOData<T>(_ obj: AudioObjectID, _ selector: AudioObjectPropertySelector,
                  _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector, mScope: scope,
        mElement: kAudioObjectPropertyElementMain
    )
    var size = UInt32(MemoryLayout<T>.size)
    let value = UnsafeMutablePointer<T>.allocate(capacity: 1)
    defer { value.deallocate() }
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, value)
    if st != noErr { return nil }
    return value.pointee
}

func defaultInputDevice() -> AudioObjectID? {
    guard let id: AudioObjectID = getAOData(
        AudioObjectID(kAudioObjectSystemObject),
        kAudioHardwarePropertyDefaultInputDevice
    ), id != kAudioObjectUnknown else { return nil }
    return id
}

// MARK: - Probe mode

if probeMicRunning {
    guard let devID = defaultInputDevice() else { exit(2) }
    guard let running: UInt32 = getAOData(
        devID, kAudioDevicePropertyDeviceIsRunningSomewhere
    ) else { exit(2) }
    exit(running != 0 ? 0 : 1)
}

// MARK: - System-audio process tap
//
// The process tap is independent of the mic: it follows system audio, not the
// input device. Only the *aggregate* references a specific mic UID, so a
// mid-capture input-device change rebuilds the aggregate and reuses this tap.

func processObjectID(forPID pid: pid_t) -> AudioObjectID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var inPID = pid
    var outObj: AudioObjectID = kAudioObjectUnknown
    var outSize = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &addr,
        UInt32(MemoryLayout<pid_t>.size), &inPID,
        &outSize, &outObj
    )
    return st == noErr ? outObj : kAudioObjectUnknown
}

let myProcObj = processObjectID(forPID: getpid())
let excludes: [AudioObjectID] = (myProcObj == kAudioObjectUnknown) ? [] : [myProcObj]

let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: excludes)
tapDesc.name = "witness-audiotap"
tapDesc.isPrivate = true
tapDesc.muteBehavior = .unmuted

var tapID: AudioObjectID = kAudioObjectUnknown
do {
    let st = AudioHardwareCreateProcessTap(tapDesc, &tapID)
    if st != noErr || tapID == kAudioObjectUnknown {
        die("AudioHardwareCreateProcessTap failed", status: st)
    }
}

guard let tapUIDRef: CFString = getAOData(tapID, kAudioTapPropertyUID) else {
    die("could not read tap UID")
}
let tapUID = tapUIDRef as String

// MARK: - Output buffers + resampler state
//
// Mutated only from ctlQ (build/rebuild) and read from the CoreAudio IOProc
// thread. Every mutation happens while the IOProc is stopped, so no callback
// can be in flight — that's what makes the plain globals safe here.

var monoMic: UnsafeMutablePointer<Float32>?
var monoSys: UnsafeMutablePointer<Float32>?
var outBuf: UnsafeMutablePointer<Float32>?
var monoCapFrames = 0
var outCapFrames = 0

// Linear-interpolation resampler, shared by both channels so they can never
// drift apart: one read position drives both, each keeping its own carry-over
// sample from the previous callback. `rsPos` is a fractional index into the
// virtual signal [prev, x[0], x[1], ... x[n-1]].
var rsRatio: Double = 1.0     // input frames consumed per output frame
var rsPos: Double = 0.0
var rsPrevMic: Float32 = 0
var rsPrevSys: Float32 = 0
var rsIdentity = true         // device rate == output rate; copy, don't resample

// Counters. `framesEmitted` is the stall watchdog's liveness signal: CoreAudio
// delivers callbacks continuously whether or not anyone is speaking, so a
// frame count that stops advancing means the device stopped, not that the room
// went quiet.
var framesEmitted: UInt64 = 0
var overrunCallbacks: UInt64 = 0
var shortWrites: UInt64 = 0

signal(SIGPIPE, SIG_IGN)

/// Write every byte, tolerating signal interruption and partial pipe writes.
/// The old code ignored `write`'s return entirely; once we upsample (16 kHz in,
/// 48 kHz out triples the byte rate) a dropped tail would desynchronise the
/// interleaved stream permanently, so account for it instead.
@inline(__always)
func writeAll(_ base: UnsafeRawPointer, _ bytes: Int) {
    var off = 0
    while off < bytes {
        let n = write(1, base.advanced(by: off), bytes - off)
        if n > 0 { off += n; continue }
        if n < 0 && errno == EINTR { continue }
        // EAGAIN (pipe full, non-blocking) or EPIPE (ffmpeg gone). Nothing
        // useful to do on the realtime thread; the stall watchdog and the
        // parent's ffmpeg supervision handle the terminal cases.
        shortWrites &+= 1
        return
    }
}

// MARK: - IOProc — downmix mic + tap, resample to outRate, interleave
//
// Buffer layout in the input AudioBufferList:
//   * buffer 0 = mic sub-device (1 or 2 channels, interleaved within itself)
//   * buffer 1 = stereo tap (2 channels)
// We average each down to mono, resample both to the fixed output rate off a
// single shared read position, and write [mic, sys, mic, sys, ...].

let ioProc: AudioDeviceIOProc = { (
    _ deviceID: AudioObjectID,
    _ inNow: UnsafePointer<AudioTimeStamp>,
    _ inInputData: UnsafePointer<AudioBufferList>,
    _ inInputTime: UnsafePointer<AudioTimeStamp>,
    _ outOutputData: UnsafeMutablePointer<AudioBufferList>,
    _ inOutputTime: UnsafePointer<AudioTimeStamp>,
    _ inClientData: UnsafeMutableRawPointer?
) -> OSStatus in
    let abl = UnsafeMutableAudioBufferListPointer(
        UnsafeMutablePointer(mutating: inInputData)
    )
    if abl.count < 2 { return noErr }

    let micBuf = abl[0]
    let tapBuf = abl[1]
    let micCh = Int(micBuf.mNumberChannels)
    let tapCh = Int(tapBuf.mNumberChannels)
    if micCh == 0 || tapCh == 0 { return noErr }

    let micFrames = Int(micBuf.mDataByteSize) / (MemoryLayout<Float32>.size * micCh)
    let tapFrames = Int(tapBuf.mDataByteSize) / (MemoryLayout<Float32>.size * tapCh)
    let frames = min(micFrames, tapFrames)
    if frames == 0 { return noErr }

    guard let micRaw = micBuf.mData, let tapRaw = tapBuf.mData,
          let mm = monoMic, let ms = monoSys, let ob = outBuf else { return noErr }
    if frames > monoCapFrames {
        // Device handed us a bigger block than we sized for. Dropping it keeps
        // the stream aligned; the rebuild path resizes on the next device change.
        overrunCallbacks &+= 1
        return noErr
    }

    let micPtr = micRaw.assumingMemoryBound(to: Float32.self)
    let tapPtr = tapRaw.assumingMemoryBound(to: Float32.self)
    let micScale: Float32 = 1.0 / Float32(micCh)
    let tapScale: Float32 = 1.0 / Float32(tapCh)

    // Downmix to mono first — the resampler needs random access to the mono
    // signal (including the sample before the current read position).
    if micCh == 1 {
        mm.update(from: micPtr, count: frames)
    } else {
        for f in 0..<frames {
            var v: Float32 = 0
            for c in 0..<micCh { v += micPtr[f * micCh + c] }
            mm[f] = v * micScale
        }
    }
    if tapCh == 1 {
        ms.update(from: tapPtr, count: frames)
    } else {
        for f in 0..<frames {
            var v: Float32 = 0
            for c in 0..<tapCh { v += tapPtr[f * tapCh + c] }
            ms[f] = v * tapScale
        }
    }

    var produced = 0
    if rsIdentity {
        produced = min(frames, outCapFrames)
        for f in 0..<produced {
            ob[f * 2]     = mm[f]
            ob[f * 2 + 1] = ms[f]
        }
        if frames > 0 {
            rsPrevMic = mm[frames - 1]
            rsPrevSys = ms[frames - 1]
        }
    } else {
        let nd = Double(frames)
        while rsPos < nd && produced < outCapFrames {
            let i = Int(rsPos)                       // 0 ..< frames
            let frac = Float32(rsPos - Double(i))
            // Virtual index i is the carry-over sample when i == 0, else x[i-1];
            // virtual index i+1 is always x[i], which is in bounds.
            let aM = (i == 0) ? rsPrevMic : mm[i - 1]
            let aS = (i == 0) ? rsPrevSys : ms[i - 1]
            ob[produced * 2]     = aM + frac * (mm[i] - aM)
            ob[produced * 2 + 1] = aS + frac * (ms[i] - aS)
            produced += 1
            rsPos += rsRatio
        }
        rsPos -= nd
        if rsPos < 0 { rsPos = 0 }
        rsPrevMic = mm[frames - 1]
        rsPrevSys = ms[frames - 1]
    }

    if produced > 0 {
        writeAll(ob, produced * 2 * MemoryLayout<Float32>.size)
        framesEmitted &+= UInt64(produced)
    }
    return noErr
}

// MARK: - Capture graph (rebuildable)

let ctlQ = DispatchQueue(label: "com.witness.audiotap.ctl")

var aggID: AudioObjectID = kAudioObjectUnknown
var ioProcID: AudioDeviceIOProcID?
var deviceRate: Double = 0
var currentMicUID: String = ""

func aggRate() -> Float64? {
    guard aggID != kAudioObjectUnknown else { return nil }
    return getAOData(aggID, kAudioDevicePropertyNominalSampleRate)
}

func setAggRate(_ rate: Float64) -> OSStatus {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var r = rate
    return AudioObjectSetPropertyData(
        aggID, &addr, 0, nil, UInt32(MemoryLayout<Float64>.size), &r
    )
}

/// Stop and destroy the IOProc + aggregate. Leaves the process tap alive.
func teardownAggregate() {
    if let p = ioProcID, aggID != kAudioObjectUnknown {
        AudioDeviceStop(aggID, p)
        AudioDeviceDestroyIOProcID(aggID, p)
    }
    ioProcID = nil
    if aggID != kAudioObjectUnknown {
        AudioHardwareDestroyAggregateDevice(aggID)
        aggID = kAudioObjectUnknown
    }
}

/// (Re)size the mono + interleaved scratch buffers for the current device rate.
/// Called only with the IOProc stopped.
func sizeBuffers(inFrames: Int) {
    let inCap = max(inFrames, 4096) * 2
    // Upsampling produces ceil(inCap / ratio) output frames; +2 covers the
    // fractional read position straddling a block boundary.
    let outCap = Int((Double(inCap) / max(rsRatio, 0.0001)).rounded(.up)) + 2

    monoMic?.deallocate()
    monoSys?.deallocate()
    outBuf?.deallocate()
    monoMic = UnsafeMutablePointer<Float32>.allocate(capacity: inCap)
    monoSys = UnsafeMutablePointer<Float32>.allocate(capacity: inCap)
    outBuf = UnsafeMutablePointer<Float32>.allocate(capacity: outCap * 2)
    monoCapFrames = inCap
    outCapFrames = outCap
}

/// Build the aggregate around the *current* default input device and start it.
/// Returns nil on success or a human-readable reason on failure.
func buildAggregate() -> String? {
    guard let micID = defaultInputDevice() else { return "no default input device" }
    guard let micUIDRef: CFString = getAOData(micID, kAudioDevicePropertyDeviceUID) else {
        return "could not read default input device UID"
    }
    let micUID = micUIDRef as String

    // Sub-device order matters: the aggregate's input streams are laid out
    // sub-devices first (in the listed order), then sub-taps. We list the mic
    // first so it occupies the leading channel(s) in the IOProc buffer list.
    let aggUID = "witness-tap-\(UUID().uuidString)"
    let aggDesc: [String: Any] = [
        kAudioAggregateDeviceNameKey: "witness-mic+tap",
        kAudioAggregateDeviceUIDKey: aggUID,
        kAudioAggregateDeviceIsPrivateKey: 1,
        kAudioAggregateDeviceIsStackedKey: 0,
        kAudioAggregateDeviceMainSubDeviceKey: micUID,
        kAudioAggregateDeviceSubDeviceListKey: [
            [kAudioSubDeviceUIDKey: micUID],
        ],
        kAudioAggregateDeviceTapListKey: [
            [
                kAudioSubTapUIDKey: tapUID,
                kAudioSubTapDriftCompensationKey: 1,
            ],
        ],
    ]

    var newAgg: AudioObjectID = kAudioObjectUnknown
    let cst = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &newAgg)
    if cst != noErr || newAgg == kAudioObjectUnknown {
        return "AudioHardwareCreateAggregateDevice failed (OSStatus=\(cst))"
    }
    aggID = newAgg
    currentMicUID = micUID

    // Prefer the output rate so the common case needs no resampling at all. A
    // CoreAudio aggregate can only adopt a rate ALL its sub-components support,
    // so this is genuinely optional: a Bluetooth headset in HFP mode pins the
    // aggregate to 16 kHz and rejects anything else. Whatever it lands on, we
    // resample to outRate — the failure that used to matter (forcing a rate the
    // device rejected, leaving mic and tap sub-streams misaligned so
    // min(micFrames, tapFrames) was 0 forever) can't happen if we don't fight it.
    if setAggRate(outRate) != noErr {
        if let actual = aggRate(), actual > 0 { _ = setAggRate(actual) }
    }
    deviceRate = Double(aggRate() ?? outRate)
    if deviceRate <= 0 { deviceRate = outRate }

    rsRatio = deviceRate / outRate
    rsIdentity = abs(deviceRate - outRate) < 0.5
    rsPos = 0
    rsPrevMic = 0
    rsPrevSys = 0

    let bufFrames: UInt32 = getAOData(aggID, kAudioDevicePropertyBufferFrameSize) ?? 512
    sizeBuffers(inFrames: Int(bufFrames))

    let pst = AudioDeviceCreateIOProcID(aggID, ioProc, nil, &ioProcID)
    if pst != noErr || ioProcID == nil {
        teardownAggregate()
        return "AudioDeviceCreateIOProcID failed (OSStatus=\(pst))"
    }
    let sst = AudioDeviceStart(aggID, ioProcID)
    if sst != noErr {
        teardownAggregate()
        return "AudioDeviceStart failed (OSStatus=\(sst))"
    }
    return nil
}

let REBUILD_ATTEMPTS = 5
let REBUILD_RETRY_S = 0.3

/// Tear down and rebuild the aggregate in place. The stdout stream is
/// unaffected: the output rate is fixed, so ffmpeg never learns that the
/// device underneath changed.
func rebuild(reason: String) {
    note("rebuilding capture graph — \(reason)")
    teardownAggregate()

    // Retry briefly. The case this exists for — a device disappearing — is
    // also the case where CoreAudio hasn't settled a replacement default input
    // yet, so the first attempt can legitimately fail with no device to bind.
    // Runs on ctlQ, so this only delays other control work, never the IOProc.
    var lastErr: String?
    for attempt in 1...REBUILD_ATTEMPTS {
        if let err = buildAggregate() {
            lastErr = err
            if attempt < REBUILD_ATTEMPTS {
                Thread.sleep(forTimeInterval: REBUILD_RETRY_S)
            }
            continue
        }
        note("rebuilt: mic=\(currentMicUID) devrate=\(Int(deviceRate)) → out=\(Int(outRate))"
             + (rsIdentity ? "" : " (resampling)"))
        return
    }

    note("FATAL rebuild failed after \(REBUILD_ATTEMPTS) attempts: \(lastErr ?? "unknown")")
    AudioHardwareDestroyProcessTap(tapID)
    exit(4)
}

// MARK: - Bring-up

if let err = buildAggregate() {
    AudioHardwareDestroyProcessTap(tapID)
    die(err)
}

// The parent parses `rate=` to pick ffmpeg's -ar. It is the rate we *emit*,
// which is fixed for the life of the process — deliberately not the device's
// rate, which can change under us. `devrate=` is diagnostic only.
note("rate=\(Int(outRate))")
note("devrate=\(Int(deviceRate))\(rsIdentity ? "" : " (resampling to \(Int(outRate)))")")

// Diagnostic: report the aggregate device's effective channel layout to
// stderr after start. Helps confirm "1 mic + 2 tap = 3 buffers" assumption.
do {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    var sz: UInt32 = 0
    AudioObjectGetPropertyDataSize(aggID, &addr, 0, nil, &sz)
    let buf = UnsafeMutableRawPointer.allocate(byteCount: Int(sz), alignment: 8)
    defer { buf.deallocate() }
    AudioObjectGetPropertyData(aggID, &addr, 0, nil, &sz, buf)
    let bl = UnsafeMutableAudioBufferListPointer(
        buf.assumingMemoryBound(to: AudioBufferList.self)
    )
    var summary = "aggregate input streams — buffers=\(bl.count)"
    for (i, b) in bl.enumerated() {
        summary += " [\(i):ch=\(b.mNumberChannels)]"
    }
    note(summary)
}

// MARK: - Default-input-device listener
//
// The aggregate pins a specific mic UID. When that device disappears (earbuds
// dying mid-meeting is the common case) CoreAudio switches the system default
// and our sub-device reference dangles: the IOProc keeps firing but delivers
// nothing usable, forever, with no error anywhere. Rebuild around the new
// default instead.

var defaultInputListenerBlock: AudioObjectPropertyListenerBlock?
do {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    // The block is dispatched on ctlQ, so it is already serialised against the
    // stall watchdog — no nested async needed.
    let block: AudioObjectPropertyListenerBlock = { _, _ in
        guard let now = defaultInputDevice(),
              let uidRef: CFString = getAOData(now, kAudioDevicePropertyDeviceUID)
        else {
            rebuild(reason: "default input device went away")
            return
        }
        let uid = uidRef as String
        if uid != currentMicUID {
            rebuild(reason: "default input device changed (\(currentMicUID) → \(uid))")
        }
    }
    defaultInputListenerBlock = block
    let st = AudioObjectAddPropertyListenerBlock(
        AudioObjectID(kAudioObjectSystemObject), &addr, ctlQ, block
    )
    if st != noErr {
        note("warning: could not observe default input device (OSStatus=\(st)); "
             + "relying on the stall watchdog for device changes")
    }
}

// MARK: - Stall watchdog
//
// Replaces a one-shot "did we ever get a frame?" check at startup. That caught
// a graph that never produced audio but was blind to one that stopped
// producing it — which is how a meeting recorded 5:45 of a 42-minute call and
// nobody noticed until the notes came out short.
//
// Callbacks arrive continuously while the device runs, silence included, so a
// static frame count is unambiguous: the device stopped. Try a rebuild first
// (recovers a device change the listener somehow missed); if that doesn't
// restore flow, exit non-zero so the parent tears the segment down and the
// daemon can start a fresh one.

let STALL_TICK_S = 2.0
let STALL_TICKS_REBUILD = 3   // ~6s of no audio → rebuild
let STALL_TICKS_FATAL = 6     // ~12s → give up, let the parent recover

var lastFrameCount: UInt64 = 0
var stallTicks = 0

let watchdog = DispatchSource.makeTimerSource(queue: ctlQ)
watchdog.schedule(deadline: .now() + STALL_TICK_S, repeating: STALL_TICK_S)
watchdog.setEventHandler {
    let seen = framesEmitted
    if seen != lastFrameCount {
        lastFrameCount = seen
        stallTicks = 0
        return
    }
    stallTicks += 1
    if stallTicks == STALL_TICKS_REBUILD {
        rebuild(reason: "no audio for ~\(Int(Double(STALL_TICKS_REBUILD) * STALL_TICK_S))s")
    } else if stallTicks >= STALL_TICKS_FATAL {
        note("FATAL no audio for ~\(Int(Double(stallTicks) * STALL_TICK_S))s after a "
             + "rebuild attempt — giving up so the parent can restart capture")
        teardownAggregate()
        AudioHardwareDestroyProcessTap(tapID)
        exit(3)
    }
}
watchdog.resume()

// MARK: - Cleanup on signal

func teardown() -> Never {
    teardownAggregate()
    AudioHardwareDestroyProcessTap(tapID)
    exit(0)
}

let termSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
termSrc.setEventHandler { teardown() }
termSrc.resume()
signal(SIGTERM, SIG_IGN)

let intSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
intSrc.setEventHandler { teardown() }
intSrc.resume()
signal(SIGINT, SIG_IGN)

RunLoop.main.run()
