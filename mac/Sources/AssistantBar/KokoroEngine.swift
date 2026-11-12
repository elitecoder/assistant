import Foundation
import KokoroSwift
import MLX
import MLXUtilsLibrary
import SessionCore

/// Runs the Kokoro voice model on this Mac: downloads it once, loads it when speaking, unloads it when idle.
final class KokoroEngine: NSObject, URLSessionDownloadDelegate {
    enum Status: Equatable {
        case notDownloaded, downloading(Double), ready, failed(String)
    }

    static let folder = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent("Library/Application Support/Assistant/Kokoro")
    static let idleUnload: TimeInterval = 300

    var onStatus: ((Status) -> Void)?
    private(set) var status: Status = .notDownloaded {
        didSet { let s = status; DispatchQueue.main.async { self.onStatus?(s) } }
    }

    private let queue = DispatchQueue(label: "assistant.kokoro", qos: .userInitiated)
    private var tts: KokoroTTS?
    private var embeddings: [String: MLXArray] = [:]
    private var unload: DispatchWorkItem?
    /// Download state, touched from the main thread and URLSession's queue, under `lock`.
    private let lock = NSLock()
    private var pending: [KokoroFile] = []
    private var current: KokoroFile?
    private lazy var session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    static var supported: Bool {
        var arm = false
        #if arch(arm64)
        arm = true
        #endif
        return arm
    }

    /// The files that fail their checksum, checked once before the first load; tests stand in a result.
    private let damaged: () -> [KokoroFile]
    private var verified = false

    init(damaged: @escaping () -> [KokoroFile] = { ModelFiles.needed(Kokoro.files, in: KokoroEngine.folder) }) {
        self.damaged = damaged
        super.init()
        if Self.installed { status = .ready }
    }

    static func path(_ file: KokoroFile) -> URL { folder.appendingPathComponent(file.name) }

    static var installed: Bool {
        Kokoro.files.allSatisfy { FileManager.default.fileExists(atPath: path($0).path) }
    }

    func download() {
        guard Self.supported else { return }
        let begin: Bool = lock.withLock {
            if case .downloading = status { return false }
            if Self.installed {
                status = .ready
                return false
            }
            pending = Kokoro.files.filter { !FileManager.default.fileExists(atPath: Self.path($0).path) }
            status = .downloading(0)
            return true
        }
        guard begin else { return }
        try? FileManager.default.createDirectory(at: Self.folder, withIntermediateDirectories: true)
        next()
    }

    private func next() {
        let file: KokoroFile? = lock.withLock {
            current = pending.isEmpty ? nil : pending.removeFirst()
            if current == nil { status = .ready }
            return current
        }
        if let file { session.downloadTask(with: file.url).resume() }
    }

    func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask, didWriteData _: Int64,
                    totalBytesWritten written: Int64, totalBytesExpectedToWrite expected: Int64) {
        lock.withLock {
            guard expected > 0, current == Kokoro.model else { return }
            status = .downloading(Double(written) / Double(expected))
        }
    }

    func urlSession(_ s: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        guard let file = lock.withLock({ current }) else { return }
        let code = (downloadTask.response as? HTTPURLResponse)?.statusCode ?? 0
        guard ModelFiles.accept(location, as: file, status: code, in: Self.folder) else {
            lock.withLock {
                status = .failed("The natural voice download was incomplete (\(file.name)). It retries next time.")
                pending = []
            }
            return
        }
        next()
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error else { return }
        lock.withLock {
            status = .failed("The natural voice didn't download: \(error.localizedDescription). It retries next time.")
            pending = []
        }
    }

    /// Speaks `text` with `voiceID` one sentence at a time. `chunk` gets 24 kHz samples; `done` reports success.
    func speak(_ text: String, voiceID: String, speed: Float = 1, isCurrent: @escaping () -> Bool,
               chunk: @escaping ([Float]) -> Void, done: @escaping (Bool) -> Void) {
        queue.async {
            self.unload?.cancel()
            defer { self.scheduleUnload() }
            guard self.load(), let key = Kokoro.npzKey(voiceID), let voice = self.embeddings[key] else {
                done(false)
                return
            }
            let language: Language = key.hasPrefix("b") ? .enGB : .enUS
            var produced = false
            for sentence in Kokoro.chunks(text) where isCurrent() {
                for samples in self.generate(sentence, voice: voice, language: language, speed: speed, depth: 0) where isCurrent() {
                    produced = true
                    chunk(samples)
                }
            }
            done(produced)
        }
    }

    private func generate(_ text: String, voice: MLXArray, language: Language, speed: Float, depth: Int) -> [[Float]] {
        do {
            let (audio, _) = try tts!.generateAudio(voice: voice, language: language, text: text, speed: speed)
            return [audio]
        } catch KokoroTTS.KokoroTTSError.tooManyTokens where depth < 6 {
            return Kokoro.halves(text).flatMap { generate($0, voice: voice, language: language, speed: speed, depth: depth + 1) }
        } catch {
            voiceLog.error("kokoro failed: \(error.localizedDescription, privacy: .public)")
            return []
        }
    }

    private func load() -> Bool {
        guard Self.supported, Self.installed else { return false }
        if !verified {
            let bad = damaged()
            guard bad.isEmpty else {
                voiceLog.error("kokoro: \(bad.map(\.name).joined(separator: ", "), privacy: .public) failed its checksum; it downloads again")
                lock.withLock { status = .notDownloaded }
                return false
            }
            verified = true
        }
        if tts == nil {
            tts = KokoroTTS(modelPath: Self.path(Kokoro.model))
            embeddings = NpyzReader.read(fileFromPath: Self.path(Kokoro.voicePack)) ?? [:]
            voiceLog.notice("kokoro loaded with \(self.embeddings.count) voices")
        }
        return tts != nil && !embeddings.isEmpty
    }

    private func scheduleUnload() {
        let work = DispatchWorkItem { [weak self] in
            self?.tts = nil
            self?.embeddings = [:]
            Memory.clearCache()
            voiceLog.notice("kokoro unloaded after idle")
        }
        unload = work
        queue.asyncAfter(deadline: .now() + Self.idleUnload, execute: work)
    }
}
