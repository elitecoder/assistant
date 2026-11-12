import Foundation

/// Gets a speech model ready and keeps one prepared value (a SpeechAnalyzer) waiting for the next question, since an
/// analyzer can't be reused once it finishes. A press never waits long: `take(within:)` gives up after its bound, so the
/// caller can fall back to the older recognizer. A failed check is retried later, and repeated failures to prepare
/// mark the model unavailable until then. Call `warm` on the main thread.
public final class ReadyPool<Ready> {
    public struct NotReady: LocalizedError {
        public var errorDescription: String? { "Apple's speech model isn't ready." }
    }

    /// True when the model can be used; reports download progress from 0 to 1 while it installs.
    public typealias Check = (_ progress: @escaping (Double) -> Void) async -> Bool

    public private(set) var state = SpeechModelState.checking
    public var onState: ((SpeechModelState) -> Void)?
    public let retryAfter: TimeInterval
    public let maximumFailures: Int
    private let check: Check
    private let make: () async throws -> Ready
    private let now: () -> Date
    private var checking: Task<Void, Never>?
    private var unavailableSince: Date?
    private var next: Task<Ready, Error>?
    private var failures = 0

    public init(retryAfter: TimeInterval = 60, maximumFailures: Int = 3, now: @escaping () -> Date = Date.init,
                check: @escaping Check, make: @escaping () async throws -> Ready) {
        self.retryAfter = retryAfter
        self.maximumFailures = maximumFailures
        self.now = now
        self.check = check
        self.make = make
    }

    private func set(_ new: SpeechModelState) {
        state = new
        onState?(new)
    }

    /// Checks for the model (again, once `retryAfter` has passed since it was unavailable) and prepares the next value.
    public func warm() {
        if state == .ready {
            if next == nil { queueNext() }
            return
        }
        guard checking == nil else { return }
        if let since = unavailableSince, now().timeIntervalSince(since) < retryAfter { return }
        set(.checking)
        checking = Task {
            let usable = await check { [weak self] fraction in
                DispatchQueue.main.async {
                    guard let self, self.checking != nil else { return }
                    self.set(.downloading(fraction))
                }
            }
            await MainActor.run {
                self.checking = nil
                if usable {
                    self.failures = 0
                    self.unavailableSince = nil
                    self.set(.ready)
                    self.queueNext()
                } else {
                    self.markUnavailable()
                }
            }
        }
    }

    private func markUnavailable() {
        unavailableSince = now()
        next = nil
        set(.unavailable)
    }

    private func queueNext() {
        let make = make
        next = Task {
            do {
                return try await make()
            } catch {
                await MainActor.run { self.failed() }
                throw error
            }
        }
    }

    private func failed() {
        failures += 1
        if failures >= maximumFailures { markUnavailable() } else if state == .ready { queueNext() }
    }

    /// The prepared value for this question, with a fresh one queued for the next. Throws `NotReady` when it isn't ready
    /// within `bound` seconds (nil waits as long as it takes), or the error preparing it failed with.
    public func take(within bound: TimeInterval?) async throws -> Ready {
        let ready = try await Self.race(bound) { () async throws -> Ready in
            let check = await MainActor.run { () -> Task<Void, Never>? in
                self.warm()
                return self.checking
            }
            await check?.value
            let task = await MainActor.run { () -> Task<Ready, Error>? in
                if self.state == .ready, self.next == nil { self.queueNext() }
                return self.state == .ready ? self.next : nil
            }
            guard let task else { throw NotReady() }
            return try await task.value
        }
        await MainActor.run {
            self.failures = 0
            self.queueNext()
        }
        return ready
    }

    /// Runs `work`, but throws `NotReady` once `bound` seconds pass, without waiting for `work` to notice.
    static func race<T>(_ bound: TimeInterval?, _ work: @escaping () async throws -> T) async throws -> T {
        guard let bound else { return try await work() }
        return try await withCheckedThrowingContinuation { continuation in
            let once = Once(continuation)
            Task { do { once.resume(.success(try await work())) } catch { once.resume(.failure(error)) } }
            Task {
                try? await Task.sleep(nanoseconds: UInt64(max(0, bound) * 1_000_000_000))
                once.resume(.failure(NotReady()))
            }
        }
    }

    private final class Once<T> {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<T, Error>?

        init(_ continuation: CheckedContinuation<T, Error>) { self.continuation = continuation }

        func resume(_ result: Result<T, Error>) {
            lock.lock()
            let pending = continuation
            continuation = nil
            lock.unlock()
            pending?.resume(with: result)
        }
    }
}
