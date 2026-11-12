import AVFoundation
import OnnxRuntimeBindings
import SessionCore

/// Downloads Smart Turn and Silero VAD once into ~/Library/Application Support/Assistant/SmartTurn, checking each
/// file's SHA-256, and loads them for ONNX Runtime.
final class TurnModels: NSObject, URLSessionDownloadDelegate {
    static let shared = TurnModels()
    static let folder = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent("Library/Application Support/Assistant/SmartTurn")

    private let lock = NSLock()
    private var downloading = false
    private var pending: [KokoroFile] = []
    private var current: KokoroFile?
    private var models: OnnxTurn?
    private lazy var session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    static func path(_ file: KokoroFile) -> URL { folder.appendingPathComponent(file.name) }

    static var installed: Bool { SmartTurn.files.allSatisfy { FileManager.default.fileExists(atPath: path($0).path) } }

    /// Starts the one-time download when the models are missing.
    func download() {
        lock.lock()
        defer { lock.unlock() }
        guard !downloading, !Self.installed else { return }
        try? FileManager.default.createDirectory(at: Self.folder, withIntermediateDirectories: true)
        pending = SmartTurn.files.filter { !FileManager.default.fileExists(atPath: Self.path($0).path) }
        downloading = true
        voiceLog.notice("turn: downloading Smart Turn and Silero VAD")
        nextLocked()
    }

    private func nextLocked() {
        guard !pending.isEmpty else {
            downloading = false
            voiceLog.notice("turn: models downloaded")
            return
        }
        current = pending.removeFirst()
        session.downloadTask(with: current!.url).resume()
    }

    func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        lock.lock()
        defer { lock.unlock() }
        guard let file = current else { return }
        let status = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200, (try? Kokoro.sha256(of: location)) == file.sha256,
              (try? FileManager.default.moveItem(at: location, to: Self.path(file))) != nil else {
            voiceLog.error("turn: \(file.name, privacy: .public) didn't download intact; the fixed pause timer stays on")
            pending = []
            downloading = false
            return
        }
        nextLocked()
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        lock.lock()
        defer { lock.unlock() }
        voiceLog.error("turn: download failed: \(error.localizedDescription, privacy: .public)")
        pending = []
        downloading = false
    }

    private let loader = DispatchQueue(label: "assistant.turn.load", qos: .utility)

    /// The models if they're already loaded, without waiting; starts loading them in the background otherwise, so a
    /// press never waits for them.
    func ready() -> OnnxTurn? {
        lock.lock()
        let models = models
        lock.unlock()
        if models == nil, Self.installed { loader.async { _ = self.loaded() } }
        return models
    }

    /// The loaded models, loading them now if needed; nil until they're downloaded (or if they fail to load).
    func loaded() -> OnnxTurn? {
        lock.lock()
        defer { lock.unlock() }
        if models == nil, Self.installed {
            let started = Date()
            models = OnnxTurn(model: Self.path(SmartTurn.model), vad: Self.path(SmartTurn.vad))
            if models != nil { voiceLog.notice("turn: Smart Turn loaded in \(milliseconds(since: started), privacy: .public)") }
        }
        return models
    }
}

/// Smart Turn and Silero VAD in ONNX Runtime. Not thread-safe: one TurnCore uses it at a time.
final class OnnxTurn {
    private let env: ORTEnv
    private let turn: ORTSession
    private let vad: ORTSession

    init?(model: URL, vad vadPath: URL) {
        do {
            env = try ORTEnv(loggingLevel: .warning)
            let options = try ORTSessionOptions()
            try options.setIntraOpNumThreads(1)
            try options.setGraphOptimizationLevel(.all)
            turn = try ORTSession(env: env, modelPath: model.path, sessionOptions: options)
            vad = try ORTSession(env: env, modelPath: vadPath.path, sessionOptions: options)
        } catch {
            voiceLog.error("turn: couldn't load the models: \(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    static func tensor(_ values: [Float], _ shape: [Int]) throws -> ORTValue {
        let data = values.withUnsafeBufferPointer { NSMutableData(bytes: $0.baseAddress, length: $0.count * 4) }
        return try ORTValue(tensorData: data, elementType: .float, shape: shape.map { NSNumber(value: $0) })
    }

    static func floats(_ value: ORTValue?) throws -> [Float] {
        guard let data = try value?.tensorData() as Data? else { return [] }
        return data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
    }

    /// The probability that the turn in these 8 seconds of 16 kHz audio is complete.
    func complete(_ window: [Float]) throws -> Float {
        let input = try Self.tensor(SmartTurn.features(window), [1, SmartTurn.melBins, SmartTurn.frames])
        let out = try turn.run(withInputs: ["input_features": input], outputNames: ["logits"], runOptions: nil)
        return try Self.floats(out["logits"]).first ?? 0
    }

    /// Silero's speech probability for one 512-sample window with the 64 samples before it, and its next state.
    func speech(_ samples: [Float], state: [Float]) throws -> (Float, [Float]) {
        var rate = Int64(SmartTurn.sampleRate)
        let sr = try ORTValue(tensorData: NSMutableData(bytes: &rate, length: 8), elementType: .int64, shape: [])
        let out = try vad.run(withInputs: ["input": try Self.tensor(samples, [1, samples.count]),
                                          "state": try Self.tensor(state, [2, 1, 128]), "sr": sr],
                              outputNames: ["output", "stateN"], runOptions: nil)
        return (try Self.floats(out["output"]).first ?? 0, try Self.floats(out["stateN"]))
    }
}

/// One question's turn detection over 16 kHz mono audio: Silero in 32 ms windows, and Smart Turn at each pause.
final class TurnCore {
    static let windowSize = 512
    static let context = 64
    /// Silero's default threshold, and the lower one that ends speech (threshold minus 0.15), as its own code uses.
    static let speechThreshold: Float = 0.5
    static let silenceThreshold: Float = 0.35

    private let models: OnnxTurn?
    private(set) var end: TurnEnd
    private(set) var audio: [Float] = []
    private var processed = 0
    /// Samples dropped from the front of `audio`, so time keeps counting from the start of the question.
    private var trimmed = 0
    private var state = [Float](repeating: 0, count: 2 * 128)
    private var speaking = false
    private(set) var lastSpeech: TimeInterval = 0
    private(set) var turnSeconds: [TimeInterval] = []
    /// When Smart Turn was asked, and what it said.
    private(set) var asks: [(at: TimeInterval, probability: Float)] = []
    /// Speech or not for each window so far, for comparing against the timer.
    private(set) var speechFlags: [Bool] = []

    init(models: OnnxTurn?) {
        self.models = models
        end = TurnEnd(modelAvailable: models != nil)
    }

    var seconds: TimeInterval { Double(processed + trimmed) / Double(SmartTurn.sampleRate) }

    /// Takes more audio; returns the end of the turn once it's reached.
    func feed(_ samples: [Float]) -> TurnEnd.Decision? {
        audio += samples
        while processed + Self.windowSize <= audio.count {
            let start = max(0, processed - Self.context)
            var window = Array(audio[start..<(processed + Self.windowSize)])
            if processed < Self.context { window = [Float](repeating: 0, count: Self.context - processed) + window }
            processed += Self.windowSize
            var probability: Float = 0
            if let models, let (p, next) = try? models.speech(window, state: state), next.count == state.count {
                probability = p
                state = next
            }
            speaking = speaking ? probability >= Self.silenceThreshold : probability >= Self.speechThreshold
            if speaking { lastSpeech = seconds }
            speechFlags.append(speaking)
            switch end.take(speech: speaking, duration: Double(Self.windowSize) / Double(SmartTurn.sampleRate)) {
            case .wait: continue
            case .done(let byModel): return .done(byModel: byModel)
            case .ask:
                let started = Date()
                let probability = (try? models?.complete(SmartTurn.lastWindow(Array(audio.prefix(processed))))) ?? 0
                turnSeconds.append(Date().timeIntervalSince(started))
                asks.append((seconds, probability))
                if case .done(let byModel) = end.answer(probability) { return .done(byModel: byModel) }
            }
        }
        if audio.count > 2 * SmartTurn.window {
            let drop = audio.count - SmartTurn.window - Self.windowSize
            audio.removeFirst(drop)
            processed -= drop
            trimmed += drop
        }
        return nil
    }
}

/// Feeds a question's microphone audio, converted to 16 kHz mono, to a TurnCore off the main thread, and calls
/// `done` on the main thread once when the turn is over.
final class TurnDetector {
    private let queue = DispatchQueue(label: "assistant.turn", qos: .userInitiated)
    private let core: TurnCore
    private let done: (Bool) -> Void
    private var converter: AVAudioConverter?
    private var finished = false
    private let target = AVAudioFormat(standardFormatWithSampleRate: Double(SmartTurn.sampleRate), channels: 1)!

    init(models: OnnxTurn, done: @escaping (_ byModel: Bool) -> Void) {
        core = TurnCore(models: models)
        self.done = done
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        guard let copy = copyBuffer(buffer) else { return }
        queue.async {
            guard !self.finished else { return }
            if self.converter?.inputFormat != copy.format { self.converter = AVAudioConverter(from: copy.format, to: self.target) }
            guard let converter = self.converter, let out = TurnDetector.convert(copy, converter, self.target),
                  let data = out.floatChannelData?[0] else { return }
            let samples = Array(UnsafeBufferPointer(start: data, count: Int(out.frameLength)))
            if case .done(let byModel)? = self.core.feed(samples) {
                self.finished = true
                voiceLog.notice("turn: over after \(String(format: "%.2f", self.core.end.silence), privacy: .public) s of silence, decided by \(byModel ? "Smart Turn" : "the time limit", privacy: .public)")
                DispatchQueue.main.async { self.done(byModel) }
            }
        }
    }

    static func convert(_ buffer: AVAudioPCMBuffer, _ converter: AVAudioConverter, _ format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * format.sampleRate / buffer.format.sampleRate) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        return out.frameLength > 0 ? out : nil
    }
}

/// Reads a sound file as 16 kHz mono and runs turn detection over it at the file's own pace, with Smart Turn and
/// with the 2.5-second timer, for `--turn-eval`.
enum TurnEval {
    struct Result {
        let model: TimeInterval?
        let modelDecided: Bool
        let timer: TimeInterval?
        let timer3: TimeInterval?
        let lastSpeech: TimeInterval
        let turnSeconds: [TimeInterval]
        let asks: [(at: TimeInterval, probability: Float)]
    }

    static func samples(_ url: URL) -> [Float]? {
        guard let file = try? AVAudioFile(forReading: url),
              let target = AVAudioFormat(standardFormatWithSampleRate: Double(SmartTurn.sampleRate), channels: 1),
              let converter = AVAudioConverter(from: file.processingFormat, to: target) else { return nil }
        var out: [Float] = []
        while let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 4096),
              (try? file.read(into: buffer)) != nil, buffer.frameLength > 0 {
            if let converted = TurnDetector.convert(buffer, converter, target), let data = converted.floatChannelData?[0] {
                out += UnsafeBufferPointer(start: data, count: Int(converted.frameLength))
            }
        }
        return out
    }

    static func run(_ samples: [Float], models: OnnxTurn) -> Result {
        let core = TurnCore(models: models)
        var modelAt: TimeInterval?
        var decided = false
        var index = 0
        while index < samples.count {
            let next = Array(samples[index..<min(samples.count, index + 1600)])
            index += next.count
            if case .done(let byModel)? = core.feed(next), modelAt == nil {
                modelAt = core.seconds
                decided = byModel
            }
        }
        // The timers hear the same voice detector, so they differ from Smart Turn only in how they decide.
        let window = Double(TurnCore.windowSize) / Double(SmartTurn.sampleRate)
        func timer(_ seconds: TimeInterval) -> TimeInterval? {
            var silence: TimeInterval = 0
            var heard = false
            for (i, speech) in core.speechFlags.enumerated() {
                if speech {
                    heard = true
                    silence = 0
                } else if heard {
                    silence += window
                    if silence >= seconds - 1e-9 { return Double(i + 1) * window }
                }
            }
            return nil
        }
        return Result(model: modelAt, modelDecided: decided, timer: timer(TurnEnd.timer), timer3: timer(3), lastSpeech: core.lastSpeech,
                      turnSeconds: core.turnSeconds, asks: core.asks)
    }
}
