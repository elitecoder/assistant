import AVFoundation
import SessionCore
import Speech

/// A copy of a microphone buffer, which the engine may reuse once its tap returns.
func copyBuffer(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else { return nil }
    copy.frameLength = buffer.frameLength
    let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
    let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
    for (from, to) in zip(source, target) {
        guard let src = from.mData, let dst = to.mData else { continue }
        dst.copyMemory(from: src, byteCount: Int(min(from.mDataByteSize, to.mDataByteSize)))
    }
    return copy
}

func milliseconds(since start: Date) -> String { String(format: "%.0f ms", Date().timeIntervalSince(start) * 1000) }

/// How long `finish` waits for the final text.
enum FinishLimit {
    /// A spoken question: long enough for the audio the recognizer still has, within RecognitionWait's bounds.
    case live
    /// A file: until the recognizer has finished all of it.
    case none
}

/// Turns microphone audio into text for one spoken question. `append` takes buffers on the audio thread; text,
/// errors, and the final result arrive on the main thread.
protocol SpeechSession: AnyObject {
    func append(_ buffer: AVAudioPCMBuffer)
    /// Ends the audio, then calls `done` once with the final text.
    func finish(_ limit: FinishLimit, _ done: @escaping (String) -> Void)
    func cancel()
    /// Buffers appended that the recognizer hasn't taken yet, so a file can be fed without loading it all at once.
    var pending: Int { get }
}

/// The older SFSpeechRecognizer, used when SpeechAnalyzer isn't available or isn't ready in time.
final class LegacySession: SpeechSession {
    private let request = SFSpeechAudioBufferRecognitionRequest()
    private var task: SFSpeechRecognitionTask?
    private var transcript = TranscriptAccumulator()
    private var finished: ((String) -> Void)?
    private var ended = false
    let pending = 0

    /// nil when the recognizer isn't available right now.
    init?(recognizer: SFSpeechRecognizer?, vocabulary: [String], onText: @escaping (String) -> Void,
          onError: @escaping (NSError) -> Void) {
        guard let recognizer, recognizer.isAvailable else { return nil }
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        request.contextualStrings = vocabulary
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, !self.ended || self.finished != nil else { return }
                if let result {
                    let segments = result.bestTranscription.segments.map {
                        TranscriptAccumulator.Segment(start: $0.timestamp, duration: $0.duration)
                    }
                    let text = self.transcript.update(result.bestTranscription.formattedString, segments: segments)
                    if result.isFinal && self.finished != nil { self.complete() } else if !self.ended { onText(text) }
                } else if let error = error as NSError? {
                    if self.finished != nil { self.complete() } else { onError(error) }
                }
            }
        }
    }

    func append(_ buffer: AVAudioPCMBuffer) { request.append(buffer) }

    func finish(_ limit: FinishLimit, _ done: @escaping (String) -> Void) {
        ended = true
        finished = done
        request.endAudio()
        guard case .live = limit else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + RecognitionWait.live(backlog: 0)) { [weak self] in self?.complete() }
    }

    private func complete() {
        guard let done = finished else { return }
        finished = nil
        task?.cancel()
        done(transcript.text)
    }

    func cancel() {
        ended = true
        finished = nil
        request.endAudio()
        task?.cancel()
    }
}

/// What AnalyzerSession needs from SpeechAnalyzer for one question, so tests can stand in for it.
@available(macOS 26, *)
protocol AnalyzerBackend: AnyObject {
    var format: AVAudioFormat { get }
    /// Starts analyzing `input`. Each result brings its text, whether it's final, and where in the audio it ends.
    func start(_ input: AsyncStream<AnalyzerInput>, result: @escaping (String, Bool, TimeInterval) -> Void) async throws
    /// Finalizes all the input and returns once the last result has arrived.
    func finish() async throws
    func cancel() async
}

/// A SpeechAnalyzer with its SpeechTranscriber, prepared ahead so a press doesn't wait for the model to load.
@available(macOS 26, *)
final class SpeechBackend: AnalyzerBackend {
    let analyzer: SpeechAnalyzer
    let transcriber: SpeechTranscriber
    let format: AVAudioFormat
    private var reader: Task<Void, Error>?

    struct NoFormat: LocalizedError {
        var errorDescription: String? { "Apple's speech model has no audio format it can take." }
    }

    private init(analyzer: SpeechAnalyzer, transcriber: SpeechTranscriber, format: AVAudioFormat) {
        self.analyzer = analyzer
        self.transcriber = transcriber
        self.format = format
    }

    static func transcriber(_ locale: Locale) -> SpeechTranscriber {
        SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [.volatileResults],
                          attributeOptions: [])
    }

    /// Passes `vocabulary` as contextual strings. SpeechTranscriber's output didn't change with them in testing.
    static func make(locale: Locale, vocabulary: [String]) async throws -> SpeechBackend {
        let started = Date()
        let transcriber = transcriber(locale)
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw NoFormat()
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber],
                                      options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        let context = AnalysisContext()
        context.contextualStrings[.general] = vocabulary
        try await analyzer.setContext(context)
        try await analyzer.prepareToAnalyze(in: format)
        voiceLog.notice("speech: prepared an analyzer in \(milliseconds(since: started), privacy: .public)")
        return SpeechBackend(analyzer: analyzer, transcriber: transcriber, format: format)
    }

    func start(_ input: AsyncStream<AnalyzerInput>, result: @escaping (String, Bool, TimeInterval) -> Void) async throws {
        let transcriber = transcriber
        reader = Task {
            for try await r in transcriber.results {
                result(String(r.text.characters), r.isFinal, r.range.end.seconds)
            }
        }
        try await analyzer.start(inputSequence: input)
    }

    func finish() async throws {
        try await analyzer.finalizeAndFinishThroughEndOfInput()
        try await reader?.value
    }

    func cancel() async { await analyzer.cancelAndFinishNow() }
}

/// Apple's newer on-device speech model: checks for it, installs it on first use with progress, and keeps a prepared
/// analyzer waiting. ReadyPool holds the retry and fallback rules.
@available(macOS 26, *)
final class AnalyzerModel {
    static let locale = Locale(identifier: "en-US")
    let pool: ReadyPool<SpeechBackend>
    private let settings = Settings()

    final class Settings: @unchecked Sendable {
        private let lock = NSLock()
        private var _locale: Locale?
        private var _vocabulary = Vocabulary.terms
        var locale: Locale? {
            get { lock.lock(); defer { lock.unlock() }; return _locale }
            set { lock.lock(); _locale = newValue; lock.unlock() }
        }
        var vocabulary: [String] {
            get { lock.lock(); defer { lock.unlock() }; return _vocabulary }
            set { lock.lock(); _vocabulary = newValue; lock.unlock() }
        }
    }

    struct Unavailable: LocalizedError {
        var errorDescription: String? { "Apple's speech model isn't available on this Mac." }
    }

    var vocabulary: [String] {
        get { settings.vocabulary }
        set { settings.vocabulary = newValue }
    }

    init() {
        let settings = settings
        pool = ReadyPool(check: { progress in await Self.check(settings, progress) }, make: {
            guard let locale = settings.locale else { throw Unavailable() }
            return try await SpeechBackend.make(locale: locale, vocabulary: settings.vocabulary)
        })
    }

    private static func check(_ settings: Settings, _ progress: @escaping (Double) -> Void) async -> Bool {
        let started = Date()
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Self.locale) else {
            voiceLog.notice("speech: SpeechTranscriber isn't available, using SFSpeechRecognizer")
            return false
        }
        let probe = SpeechBackend.transcriber(locale)
        let status = await AssetInventory.status(forModules: [probe])
        if status == .unsupported {
            voiceLog.notice("speech: no SpeechTranscriber model for \(locale.identifier, privacy: .public)")
            return false
        }
        if status < .installed {
            do {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
                    progress(0)
                    let observation = request.progress.observe(\.fractionCompleted) { p, _ in progress(p.fractionCompleted) }
                    defer { observation.invalidate() }
                    try await request.downloadAndInstall()
                    voiceLog.notice("speech: installed the model after \(milliseconds(since: started), privacy: .public)")
                }
            } catch {
                voiceLog.error("speech: model install failed, trying again later: \(error.localizedDescription, privacy: .public)")
                return false
            }
        }
        settings.locale = locale
        voiceLog.notice("speech: SpeechTranscriber ready after \(milliseconds(since: started), privacy: .public)")
        return true
    }
}

/// One question through SpeechAnalyzer. Microphone buffers are copied as they arrive, converted to the analyzer's
/// format, and streamed in; audio that arrives before the analyzer is ready waits in the stream, so none is lost. If
/// the analyzer can't be had, the audio so far goes to `fallback` (the older recognizer), which takes over.
@available(macOS 26, *)
final class AnalyzerSession: SpeechSession {
    private let raw: AsyncStream<AVAudioPCMBuffer>.Continuation
    private var live = LiveTranscript()
    private var feeder: Task<Void, Never>?
    private var backend: AnalyzerBackend?
    private var finished: ((String) -> Void)?
    private var limit = FinishLimit.live
    private var cancelled = false
    private let onText: (String) -> Void
    private let onError: (NSError) -> Void
    private let fallback: () -> SpeechSession?

    /// Shared with the audio thread, under `lock`.
    private let lock = NSLock()
    private var held: [AVAudioPCMBuffer] = []
    private var started = false
    private var forward: SpeechSession?
    private var queued = 0
    private var queuedSeconds: TimeInterval = 0
    private var fedSeconds: TimeInterval = 0
    private var processedSeconds: TimeInterval = 0

    struct NoConverter: LocalizedError {
        let from: AVAudioFormat
        var errorDescription: String? { "Assistant can't convert this microphone's audio (\(Int(from.sampleRate)) Hz)." }
    }

    init(take: @escaping () async throws -> AnalyzerBackend, fallback: @escaping () -> SpeechSession?,
         onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) {
        self.onText = onText
        self.onError = onError
        self.fallback = fallback
        let (stream, raw) = AsyncStream<AVAudioPCMBuffer>.makeStream()
        self.raw = raw
        let began = Date()
        // Holds the session until its audio ends with `finish` or `cancel`.
        feeder = Task {
            let backend: AnalyzerBackend
            do {
                backend = try await take()
            } catch {
                await MainActor.run { self.fallBack(error) }
                return
            }
            await self.run(backend, stream, began: began)
        }
    }

    var pending: Int {
        lock.lock()
        defer { lock.unlock() }
        return forward?.pending ?? queued
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        defer { lock.unlock() }
        if let forward { return forward.append(buffer) }
        guard let copy = copyBuffer(buffer) else { return }
        if !started { held.append(copy) }
        queued += 1
        queuedSeconds += Double(copy.frameLength) / copy.format.sampleRate
        raw.yield(copy)
    }

    /// The analyzer isn't ready: hand the audio so far to the older recognizer and pass everything to it from now on.
    private func fallBack(_ error: Error) {
        guard !cancelled else { return }
        voiceLog.notice("speech: SpeechAnalyzer not ready (\(error.localizedDescription, privacy: .public)), using SFSpeechRecognizer")
        guard let other = fallback() else { return fail(error) }
        lock.lock()
        forward = other
        let replay = held
        held = []
        replay.forEach(other.append)
        lock.unlock()
        raw.finish()
        if let done = finished {
            finished = nil
            other.finish(limit, done)
        }
    }

    private func run(_ backend: AnalyzerBackend, _ stream: AsyncStream<AVAudioPCMBuffer>, began: Date) async {
        let proceed = await MainActor.run { () -> Bool in
            self.backend = backend
            return !self.cancelled
        }
        guard proceed else { return await backend.cancel() }
        let (input, feed) = AsyncStream<AnalyzerInput>.makeStream()
        do {
            try await backend.start(input) { text, isFinal, end in
                DispatchQueue.main.async { self.took(text, isFinal: isFinal, end: end) }
            }
        } catch {
            await MainActor.run { self.fail(error) }
            return
        }
        lock.withLock {
            started = true
            held = []
        }
        voiceLog.notice("speech: analyzer started \(milliseconds(since: began), privacy: .public) after the question began")
        var converter: AVAudioConverter?
        var source: AVAudioFormat?
        for await buffer in stream {
            lock.withLock {
                queued -= 1
                queuedSeconds -= Double(buffer.frameLength) / buffer.format.sampleRate
            }
            if source == nil {
                source = buffer.format
                if buffer.format != backend.format {
                    converter = AVAudioConverter(from: buffer.format, to: backend.format)
                    if converter == nil {
                        await MainActor.run { self.fail(NoConverter(from: buffer.format)) }
                        feed.finish()
                        return await backend.cancel()
                    }
                }
            }
            guard buffer.format == source else { continue }
            guard let out = converter.map({ Self.convert(buffer, with: $0, to: backend.format) }) ?? buffer
            else { continue }
            yield(out, to: feed)
        }
        if let converter, let tail = Self.flush(converter, to: backend.format) { yield(tail, to: feed) }
        feed.finish()
        do {
            try await backend.finish()
        } catch is CancellationError {
        } catch {
            voiceLog.error("speech: finishing failed: \(error.localizedDescription, privacy: .public)")
        }
        await MainActor.run { self.complete() }
    }

    private func yield(_ buffer: AVAudioPCMBuffer, to feed: AsyncStream<AnalyzerInput>.Continuation) {
        lock.lock()
        fedSeconds += Double(buffer.frameLength) / buffer.format.sampleRate
        lock.unlock()
        feed.yield(AnalyzerInput(buffer: buffer))
    }

    static func convert(_ buffer: AVAudioPCMBuffer, with converter: AVAudioConverter, to format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if consumed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        return status == .error || out.frameLength == 0 ? nil : out
    }

    /// The samples the converter still holds back for resampling, at the end of the audio.
    static func flush(_ converter: AVAudioConverter, to format: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4096) else { return nil }
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            inputStatus.pointee = .endOfStream
            return nil
        }
        return status == .error || out.frameLength == 0 ? nil : out
    }

    private func took(_ text: String, isFinal: Bool, end: TimeInterval) {
        let all = live.take(text, isFinal: isFinal)
        lock.lock()
        if end.isFinite { processedSeconds = max(processedSeconds, end) }
        lock.unlock()
        if finished == nil && !cancelled { onText(all) }
    }

    private func fail(_ error: Error) {
        voiceLog.error("speech: analyzer failed: \(error.localizedDescription, privacy: .public)")
        if finished != nil { return complete() }
        if !cancelled { onError(error as NSError) }
    }

    private func complete() {
        guard let done = finished else { return }
        finished = nil
        done(live.text)
    }

    /// Audio the analyzer hasn't turned into results yet, in seconds.
    var backlog: TimeInterval {
        lock.lock()
        defer { lock.unlock() }
        return queuedSeconds + fedSeconds - processedSeconds
    }

    func finish(_ limit: FinishLimit, _ done: @escaping (String) -> Void) {
        lock.lock()
        let other = forward
        lock.unlock()
        if let other { return other.finish(limit, done) }
        self.limit = limit
        finished = done
        raw.finish()
        guard case .live = limit else { return }
        let wait = RecognitionWait.live(backlog: backlog)
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in
            guard let self, self.finished != nil else { return }
            voiceLog.notice("speech: final result took over \(wait) s, using what was heard")
            self.complete()
        }
    }

    func cancel() {
        cancelled = true
        finished = nil
        raw.finish()
        lock.lock()
        let other = forward
        lock.unlock()
        other?.cancel()
        if let backend { Task { await backend.cancel() } }
    }
}
