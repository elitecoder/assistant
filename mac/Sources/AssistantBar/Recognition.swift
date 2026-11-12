import AVFoundation
import SessionCore
import Speech

func milliseconds(since start: Date) -> String { String(format: "%.0f ms", Date().timeIntervalSince(start) * 1000) }

/// Turns microphone audio into text for one spoken question. `append` takes buffers on the audio thread; text,
/// errors, and the final result arrive on the main thread.
protocol SpeechSession: AnyObject {
    func append(_ buffer: AVAudioPCMBuffer)
    /// Ends the audio, then calls `done` once with the final text.
    func finish(_ done: @escaping (String) -> Void)
    func cancel()
}

/// The older SFSpeechRecognizer, used when SpeechAnalyzer isn't available. After a pause it can start a fresh
/// transcript, so TranscriptAccumulator keeps the words heard before the pause.
final class LegacySession: SpeechSession {
    private let request = SFSpeechAudioBufferRecognitionRequest()
    private var task: SFSpeechRecognitionTask?
    private var transcript = TranscriptAccumulator()
    private var finished: ((String) -> Void)?
    private var ended = false

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
                    let text = self.transcript.update(result.bestTranscription.formattedString)
                    if result.isFinal && self.finished != nil { self.complete() } else if !self.ended { onText(text) }
                } else if let error = error as NSError? {
                    if self.finished != nil { self.complete() } else { onError(error) }
                }
            }
        }
    }

    func append(_ buffer: AVAudioPCMBuffer) { request.append(buffer) }

    func finish(_ done: @escaping (String) -> Void) {
        ended = true
        finished = done
        request.endAudio()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.complete() }
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

/// A SpeechAnalyzer with its SpeechTranscriber, prepared ahead so a press doesn't wait for the model to load.
@available(macOS 26, *)
struct ReadyAnalyzer {
    let analyzer: SpeechAnalyzer
    let transcriber: SpeechTranscriber
    let format: AVAudioFormat
}

/// Apple's newer on-device speech model. Installs it on first use, then keeps one prepared analyzer waiting for the
/// next question, since an analyzer can't be reused once it finishes.
@available(macOS 26, *)
final class AnalyzerModel {
    static let locale = Locale(identifier: "en-US")
    private(set) var state = SpeechModelState.checking
    var onState: ((SpeechModelState) -> Void)?
    var vocabulary: [String] = Vocabulary.terms
    private var locale: Locale?
    private var checking: Task<Void, Never>?
    private var next: Task<ReadyAnalyzer, Error>?
    private var progress: NSKeyValueObservation?

    struct Unavailable: LocalizedError {
        var errorDescription: String? { "Apple's speech model isn't available on this Mac." }
    }

    private func set(_ new: SpeechModelState) {
        DispatchQueue.main.async {
            if case .downloading = new, self.state == .ready || self.state == .unavailable { return }
            self.state = new
            self.onState?(new)
        }
    }

    /// Checks for the model, installs it if it's missing, and prepares the next analyzer. Call it on the main thread.
    func warm() {
        if checking == nil {
            checking = Task { await self.check() }
        } else if state == .ready, next == nil {
            queueNext()
        }
    }

    private func check() async {
        let started = Date()
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Self.locale) else {
            voiceLog.notice("speech: SpeechTranscriber isn't available, using SFSpeechRecognizer")
            return set(.unavailable)
        }
        let probe = Self.transcriber(locale)
        let status = await AssetInventory.status(forModules: [probe])
        if status == .unsupported {
            voiceLog.notice("speech: no SpeechTranscriber model for \(locale.identifier, privacy: .public)")
            return set(.unavailable)
        }
        if status < .installed {
            do {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
                    set(.downloading(0))
                    progress = request.progress.observe(\.fractionCompleted) { [weak self] p, _ in
                        self?.set(.downloading(p.fractionCompleted))
                    }
                    try await request.downloadAndInstall()
                    progress = nil
                    voiceLog.notice("speech: installed the model after \(milliseconds(since: started), privacy: .public)")
                }
            } catch {
                progress = nil
                voiceLog.error("speech: model install failed: \(error.localizedDescription, privacy: .public)")
                return set(.unavailable)
            }
        }
        await MainActor.run {
            self.locale = locale
            self.state = .ready
            self.onState?(.ready)
            self.queueNext()
        }
        voiceLog.notice("speech: SpeechTranscriber ready after \(milliseconds(since: started), privacy: .public)")
    }

    static func transcriber(_ locale: Locale) -> SpeechTranscriber {
        SpeechTranscriber(locale: locale, transcriptionOptions: [], reportingOptions: [.volatileResults],
                          attributeOptions: [])
    }

    private func queueNext() {
        guard let locale else { return }
        let vocabulary = vocabulary
        next = Task {
            let started = Date()
            let transcriber = Self.transcriber(locale)
            guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
                throw Unavailable()
            }
            let analyzer = SpeechAnalyzer(modules: [transcriber],
                                          options: .init(priority: .userInitiated, modelRetention: .processLifetime))
            let context = AnalysisContext()
            context.contextualStrings[.general] = vocabulary
            try await analyzer.setContext(context)
            try await analyzer.prepareToAnalyze(in: format)
            voiceLog.notice("speech: prepared an analyzer in \(milliseconds(since: started), privacy: .public)")
            return ReadyAnalyzer(analyzer: analyzer, transcriber: transcriber, format: format)
        }
    }

    /// The prepared analyzer for this question, and a fresh one queued for the next. Waits for the model check when
    /// it's still running.
    func take() async throws -> ReadyAnalyzer {
        let check = await MainActor.run { self.checking }
        await check?.value
        let ready = await MainActor.run { () -> Task<ReadyAnalyzer, Error>? in
            if self.next == nil { self.queueNext() }
            let taken = self.next
            self.queueNext()
            return taken
        }
        guard let ready else { throw Unavailable() }
        return try await ready.value
    }
}

/// One question through SpeechAnalyzer. Microphone buffers are copied as they arrive, converted to the analyzer's
/// format, and streamed in; audio that arrives before the analyzer is ready waits in the stream, so none is lost.
@available(macOS 26, *)
final class AnalyzerSession: SpeechSession {
    private let raw: AsyncStream<AVAudioPCMBuffer>.Continuation
    private var live = LiveTranscript()
    private var feeder: Task<Void, Never>?
    private var reader: Task<Void, Error>?
    private var analyzer: SpeechAnalyzer?
    private var finished: ((String) -> Void)?
    private var cancelled = false
    private let onText: (String) -> Void
    private let onError: (NSError) -> Void

    init(model: AnalyzerModel, onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) {
        self.onText = onText
        self.onError = onError
        let (stream, raw) = AsyncStream<AVAudioPCMBuffer>.makeStream()
        self.raw = raw
        let started = Date()
        // Holds the session until its audio ends with `finish` or `cancel`.
        feeder = Task {
            do {
                let ready = try await model.take()
                await self.run(ready, stream, started: started)
            } catch {
                await MainActor.run { self.fail(error) }
            }
        }
    }

    private func run(_ ready: ReadyAnalyzer, _ stream: AsyncStream<AVAudioPCMBuffer>, started: Date) async {
        let (input, feed) = AsyncStream<AnalyzerInput>.makeStream()
        let alreadyCancelled = await MainActor.run { () -> Bool in
            self.analyzer = ready.analyzer
            return self.cancelled
        }
        if alreadyCancelled { return await ready.analyzer.cancelAndFinishNow() }
        reader = Task {
            for try await result in ready.transcriber.results {
                let text = String(result.text.characters)
                await MainActor.run { self.took(text, isFinal: result.isFinal) }
            }
        }
        do {
            try await ready.analyzer.start(inputSequence: input)
        } catch {
            await MainActor.run { self.fail(error) }
            return
        }
        voiceLog.notice("speech: analyzer started \(milliseconds(since: started), privacy: .public) after the question began")
        var converter: AVAudioConverter?
        for await buffer in stream {
            if converter?.inputFormat != buffer.format {
                converter = buffer.format == ready.format ? nil : AVAudioConverter(from: buffer.format, to: ready.format)
            }
            guard let out = converter.map({ Self.convert(buffer, with: $0, to: ready.format) }) ?? buffer else { continue }
            feed.yield(AnalyzerInput(buffer: out))
        }
        feed.finish()
        do {
            try await ready.analyzer.finalizeAndFinishThroughEndOfInput()
            try await reader?.value
        } catch is CancellationError {
        } catch {
            voiceLog.error("speech: finishing failed: \(error.localizedDescription, privacy: .public)")
        }
        await MainActor.run { self.complete() }
    }

    static func convert(_ buffer: AVAudioPCMBuffer, with converter: AVAudioConverter, to format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let ratio = format.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 16
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

    static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
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

    func append(_ buffer: AVAudioPCMBuffer) {
        if let copy = Self.copy(buffer) { raw.yield(copy) }
    }

    private func took(_ text: String, isFinal: Bool) {
        let all = live.take(text, isFinal: isFinal)
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

    func finish(_ done: @escaping (String) -> Void) {
        finished = done
        raw.finish()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self, self.finished != nil else { return }
            voiceLog.notice("speech: final result took over 2 s, using what was heard")
            self.complete()
        }
    }

    func cancel() {
        cancelled = true
        finished = nil
        raw.finish()
        if let analyzer { Task { await analyzer.cancelAndFinishNow() } }
    }
}
