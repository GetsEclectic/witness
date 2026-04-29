// witness-audiotap: macOS mic + system-audio capture for witness.
//
// Builds a private CoreAudio aggregate device combining:
//   * the user's default input device (mic) as a sub-device
//   * a system-audio process tap (excluding our own PID) as a sub-tap
// and writes interleaved Float32 PCM to stdout at 48 kHz, 2 channels:
//   * channel 0 = mic (downmixed to mono if the device is multi-channel)
//   * channel 1 = system audio (downmixed to mono from the stereo tap)
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

var sampleRate: Double = 48000
var probeMicRunning = false

do {
    var it = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = it.next() {
        switch arg {
        case "--rate":
            guard let v = it.next(), let d = Double(v) else { fatalError("--rate needs a number") }
            sampleRate = d
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
            print("CoreAudio aggregate device and writes interleaved Float32 PCM to stdout.")
            print("macOS 14.2+.")
            exit(0)
        default:
            FileHandle.standardError.write(Data("witness-audiotap: unknown arg \(arg)\n".utf8))
            exit(2)
        }
    }
}

// MARK: - Helpers

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

// MARK: - Probe mode

if probeMicRunning {
    guard let devID: AudioObjectID = getAOData(
        AudioObjectID(kAudioObjectSystemObject),
        kAudioHardwarePropertyDefaultInputDevice
    ), devID != kAudioObjectUnknown else { exit(2) }

    guard let running: UInt32 = getAOData(
        devID, kAudioDevicePropertyDeviceIsRunningSomewhere
    ) else { exit(2) }
    exit(running != 0 ? 0 : 1)
}

// MARK: - Resolve default mic UID

guard let micID: AudioObjectID = getAOData(
    AudioObjectID(kAudioObjectSystemObject),
    kAudioHardwarePropertyDefaultInputDevice
), micID != kAudioObjectUnknown else {
    die("no default input device")
}

guard let micUID: CFString = getAOData(micID, kAudioDevicePropertyDeviceUID) else {
    die("could not read default input device UID")
}

// MARK: - Build the system-audio process tap

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

guard let tapUID: CFString = getAOData(tapID, kAudioTapPropertyUID) else {
    die("could not read tap UID")
}

// MARK: - Aggregate device combining the mic + the tap
//
// Sub-device order matters: the aggregate's input streams are laid out
// sub-devices first (in the listed order), then sub-taps. We list the mic
// first so it occupies the leading channel(s) in the IOProc buffer list.

let aggUID = "witness-tap-\(UUID().uuidString)"
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "witness-mic+tap",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceIsPrivateKey: 1,
    kAudioAggregateDeviceIsStackedKey: 0,
    kAudioAggregateDeviceMainSubDeviceKey: micUID as String,
    kAudioAggregateDeviceSubDeviceListKey: [
        [kAudioSubDeviceUIDKey: micUID as String],
    ],
    kAudioAggregateDeviceTapListKey: [
        [
            kAudioSubTapUIDKey: tapUID as String,
            kAudioSubTapDriftCompensationKey: 1,
        ],
    ],
]

var aggID: AudioObjectID = kAudioObjectUnknown
do {
    let st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
    if st != noErr || aggID == kAudioObjectUnknown {
        die("AudioHardwareCreateAggregateDevice failed", status: st)
    }
}

// Force the aggregate device to 48 kHz so the output rate is predictable
// regardless of the mic's native rate (built-in mic is often 16k–48k,
// USB interfaces are 44.1k or 48k). The aggregate device handles
// resampling between sub-streams and the master rate.
do {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var newRate: Float64 = sampleRate
    let st = AudioObjectSetPropertyData(
        aggID, &addr, 0, nil,
        UInt32(MemoryLayout<Float64>.size), &newRate
    )
    if st != noErr {
        // Non-fatal: device may stick with its default rate. The downstream
        // pipeline tolerates whatever rate ffmpeg ends up seeing, since we
        // include `aresample` in the filter graph.
        FileHandle.standardError.write(Data(
            "witness-audiotap: warning: could not set aggregate rate to \(sampleRate) (OSStatus=\(st))\n".utf8
        ))
    }
}

// MARK: - IOProc — downmix mic + tap to a 2-channel interleaved PCM stream
//
// Buffer layout in the input AudioBufferList:
//   * first (nBuffers - 2) buffers belong to the mic sub-device
//     (1 channel each, non-interleaved)
//   * last 2 buffers belong to the stereo tap (1 channel each)
// We average the mic channels into a single mono stream, average the
// 2 tap channels into a single mono stream, and write [mic, sys, mic, sys, ...].

let MAX_INTERLEAVED_FLOATS = 16384
var scratch = [Float32](repeating: 0, count: MAX_INTERLEAVED_FLOATS)

signal(SIGPIPE, SIG_IGN)

// IOProc layout assumption (verified by the stderr summary above):
// the aggregate device exposes its sub-device + sub-tap as separate
// AudioBuffers. Each buffer is interleaved by channel within itself.
// Buffer 0 = mic (1 or 2 channels), buffer 1 = tap (2 channels). We
// downmix each to mono and emit a 2-channel interleaved [mic, sys] stream.

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
    if frames * 2 > MAX_INTERLEAVED_FLOATS { return noErr }

    guard let micRaw = micBuf.mData,
          let tapRaw = tapBuf.mData else { return noErr }
    let micPtr = micRaw.assumingMemoryBound(to: Float32.self)
    let tapPtr = tapRaw.assumingMemoryBound(to: Float32.self)
    let micScale: Float32 = 1.0 / Float32(micCh)
    let tapScale: Float32 = 1.0 / Float32(tapCh)

    scratch.withUnsafeMutableBufferPointer { out in
        for f in 0..<frames {
            var mic: Float32 = 0
            for c in 0..<micCh { mic += micPtr[f * micCh + c] }
            mic *= micScale

            var sys: Float32 = 0
            for c in 0..<tapCh { sys += tapPtr[f * tapCh + c] }
            sys *= tapScale

            out[f * 2]     = mic
            out[f * 2 + 1] = sys
        }
        _ = write(1, out.baseAddress, frames * 2 * MemoryLayout<Float32>.size)
    }
    return noErr
}

var ioProcID: AudioDeviceIOProcID?
do {
    let st = AudioDeviceCreateIOProcID(aggID, ioProc, nil, &ioProcID)
    if st != noErr || ioProcID == nil {
        die("AudioDeviceCreateIOProcID failed", status: st)
    }
}

do {
    let st = AudioDeviceStart(aggID, ioProcID)
    if st != noErr { die("AudioDeviceStart failed", status: st) }
}

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
    var summary = "witness-audiotap: aggregate input streams — buffers=\(bl.count)"
    for (i, b) in bl.enumerated() {
        summary += " [\(i):ch=\(b.mNumberChannels)]"
    }
    summary += "\n"
    FileHandle.standardError.write(Data(summary.utf8))
}

// MARK: - Cleanup on signal

func teardown() -> Never {
    if let p = ioProcID {
        AudioDeviceStop(aggID, p)
        AudioDeviceDestroyIOProcID(aggID, p)
    }
    AudioHardwareDestroyAggregateDevice(aggID)
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
