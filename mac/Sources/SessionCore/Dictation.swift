import Foundation

/// A sample rate and a channel count, the parts of an audio format that decide whether a tap fits.
public struct AudioShape: Equatable {
    public let sampleRate: Double
    public let channels: UInt32

    public init(sampleRate: Double, channels: UInt32) {
        self.sampleRate = sampleRate
        self.channels = channels
    }
}

extension Microphones {
    /// The shape to tap a microphone in. After AVAudioEngine switches to another device it can keep reporting the
    /// previous device's format: AirPods in call mode run at 24 kHz while the engine still says 48 kHz, and a tap at
    /// 48 kHz then fails with a format mismatch. So the device's own input rate wins, with no more channels than the
    /// device has. nil when neither source has a usable shape yet.
    public static func tapShape(device: AudioShape?, engine: AudioShape) -> AudioShape? {
        guard let device, usable(sampleRate: device.sampleRate, channels: device.channels) else {
            return usable(sampleRate: engine.sampleRate, channels: engine.channels) ? engine : nil
        }
        let channels = engine.channels > 0 ? min(engine.channels, device.channels) : device.channels
        return AudioShape(sampleRate: device.sampleRate, channels: channels)
    }
}

extension Microphones {
    /// How long a microphone may send pure silence before Assistant tries the next one. A Bluetooth headset sends
    /// zeros while it switches into call mode, so it gets longer.
    public static func silenceAllowance(_ mic: Microphone) -> TimeInterval {
        mic.bluetooth ? 3 : 0.7
    }
}

extension Microphones {
    /// Whether a tap still matches its device after the device's configuration changed. A changed rate, fewer
    /// channels, or a device that's gone means the engine has to be built again.
    public static func tapStillFits(_ tap: AudioShape, device: AudioShape?) -> Bool {
        guard let device else { return false }
        return device.sampleRate == tap.sampleRate && device.channels >= tap.channels
    }
}

/// The live text of one spoken question from Apple's SpeechTranscriber. Finalized results never change again, so
/// they're kept in order; the latest volatile result is a guess at the words after them and replaces the previous
/// guess. A pause doesn't lose anything, because what was finalized before it stays.
public struct LiveTranscript: Equatable {
    public private(set) var finalized = ""
    public private(set) var volatile = ""

    public init() {}

    /// Takes one result and returns the full text heard so far.
    @discardableResult
    public mutating func take(_ result: String, isFinal: Bool) -> String {
        if isFinal {
            finalized = Self.join(finalized, result)
            volatile = ""
        } else {
            volatile = result.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return text
    }

    public var text: String { Self.join(finalized, volatile) }

    static func join(_ a: String, _ b: String) -> String {
        [a, b].map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }.joined(separator: " ")
    }
}

/// Words the operator says that a general speech model gets wrong, for the recognizer's contextual strings.
public enum Vocabulary {
    public static let terms = ["PR", "pull request", "Assembler", "Kokoro", "Haiku", "Sonnet", "Opus", "Claude", "cmux",
                               "workspace"]
    public static let limit = 100
    /// Longer titles, such as a pasted log, aren't useful as one phrase; their words still count.
    public static let longestPhrase = 60
    static let common: Set<String> = ["the", "and", "for", "with", "from", "into", "that", "this", "fix", "add", "new",
                                      "not", "are", "was", "but", "all", "use", "via", "out", "off", "its"]

    /// The fixed terms, then each workspace title, then the distinctive words in those titles, without repeats
    /// (ignoring case) and at most `limit` of them.
    public static func contextualStrings(titles: [String], limit: Int = Vocabulary.limit) -> [String] {
        let cleaned = titles.map { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ") }.filter { !$0.isEmpty }
        let phrases = cleaned.filter { $0.count <= longestPhrase }
        let words = cleaned.flatMap { title in
            title.split(whereSeparator: { !$0.isLetter && !$0.isNumber && $0 != "-" && $0 != "'" })
                .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: "-'")) }
                .filter { $0.count >= 3 && $0.count <= longestPhrase && !$0.allSatisfy(\.isNumber)
                    && !common.contains($0.lowercased()) }
        }
        var seen = Set<String>()
        var out: [String] = []
        for phrase in terms + phrases + words where out.count < limit && seen.insert(phrase.lowercased()).inserted {
            out.append(phrase)
        }
        return out
    }
}

public enum Replay {
    /// The answer "Play again" speaks: the newest assistant answer with any text. A reopened chat shows a failed
    /// turn's "[No reply: …]" note as an answer; that isn't one to read aloud.
    public static func target(_ chat: [ChatTurn]) -> String? {
        chat.last {
            $0.role == .assistant && !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && !$0.text.hasPrefix(TurnRecord.noReplyPrefix)
        }?.text
    }
}

/// Where Apple's newer on-device speech model is in getting ready.
public enum SpeechModelState: Equatable {
    case checking
    case downloading(Double)
    case ready
    case unavailable
}

public enum SpeechRecognizerChoice: Equatable {
    case analyzer
    case legacy

    /// SpeechAnalyzer when the Mac has it and its model isn't missing or still downloading; else the older
    /// SFSpeechRecognizer. While the model is still being checked, SpeechAnalyzer: the audio waits for it.
    public static func choose(analyzerOS: Bool, model: SpeechModelState) -> SpeechRecognizerChoice {
        guard analyzerOS else { return .legacy }
        switch model {
        case .checking, .ready: return .analyzer
        case .downloading, .unavailable: return .legacy
        }
    }
}

public enum Access: Equatable {
    case granted
    case denied
    case undetermined
}

public enum VoicePermissions {
    public enum Next: Equatable {
        /// Start listening right away, with no permission request.
        case listen
        /// Ask macOS first; the answer comes back later.
        case ask
        case problem(String)
    }

    public static let micProblem =
        "Assistant can't use the microphone. Allow it in System Settings, Privacy & Security, Microphone."
    public static let speechProblem =
        "Assistant can't use speech recognition. Allow it in System Settings, Privacy & Security, Speech Recognition."

    /// SpeechAnalyzer needs only the microphone; the older recognizer also needs speech recognition access.
    public static func next(mic: Access, speech: Access, needsSpeech: Bool) -> Next {
        if mic == .denied { return .problem(micProblem) }
        if needsSpeech && speech == .denied { return .problem(speechProblem) }
        return mic == .granted && (!needsSpeech || speech == .granted) ? .listen : .ask
    }
}

public enum ListeningText {
    /// "Starting…" until the microphone's first sound arrives, then "Listening…" until words do.
    public static func label(heard: String, soundArrived: Bool) -> String {
        if !heard.isEmpty { return heard }
        return soundArrived ? "Listening…" : "Starting…"
    }

    /// The note under the listening row about the newer speech model, if any.
    public static func modelNote(_ state: SpeechModelState) -> String? {
        guard case .downloading(let fraction) = state else { return nil }
        let percent = fraction.isFinite ? Int((min(1, max(0, fraction)) * 100).rounded()) : 0
        return "Downloading Apple's speech model (\(percent)%). Until it's ready, I use the older recognizer."
    }
}

/// Decides when a microphone that sends only silence gets skipped. Its clock starts when the microphone has actually
/// started, so a buffer that arrives earlier, or one meant for the previous question, is never judged against the
/// wrong start time or the wrong allowance.
public struct SilenceWatch: Equatable {
    public enum Verdict: Equatable {
        /// The microphone hasn't started yet, so the buffer doesn't count.
        case ignored
        case waiting
        /// The first buffer with any sound in it.
        case firstSound
        case sound
        /// Silence for longer than the allowance: try the next microphone.
        case skip
    }

    public let allowance: TimeInterval
    public private(set) var startedAt: Date?
    public private(set) var heardSound = false

    public init(allowance: TimeInterval) { self.allowance = allowance }

    public mutating func started(at time: Date) { startedAt = time }

    public mutating func took(silent: Bool, at time: Date) -> Verdict {
        guard let startedAt else { return .ignored }
        if !silent {
            defer { heardSound = true }
            return heardSound ? .sound : .firstSound
        }
        if heardSound { return .sound }
        return time.timeIntervalSince(startedAt) >= allowance ? .skip : .waiting
    }
}

public enum RecognitionWait {
    public static let minimum: TimeInterval = 2
    public static let ceiling: TimeInterval = 30

    /// How long to wait for the final text after a spoken question ends: at least 2 seconds, more when the recognizer
    /// still has audio to get through (half again as long as that audio), and never more than 30 seconds.
    public static func live(backlog: TimeInterval) -> TimeInterval {
        let backlog = backlog.isFinite ? max(0, backlog) : 0
        return min(ceiling, max(minimum, backlog * 1.5))
    }
}

/// Spoken questions and spoken answers, kept apart: a question heard while an answer is running waits its turn, and an
/// answer never plays into an open microphone.
public struct VoiceTurns: Equatable {
    public private(set) var queued: [String] = []
    public private(set) var deferredReply: String?

    public static let queuedNote = "I'll ask that when the current answer is done."
    public static let deferredNote = "I'll read the answer when you finish talking."

    public init() {}

    public enum Heard: Equatable {
        case ask(String)
        case queued(note: String)
    }

    public mutating func heard(_ question: String, asking: Bool) -> Heard {
        guard asking else { return .ask(question) }
        queued.append(question)
        return .queued(note: Self.queuedNote)
    }

    /// The next question to ask once an answer is done.
    public mutating func answerFinished() -> String? {
        queued.isEmpty ? nil : queued.removeFirst()
    }

    public enum Talk: Equatable {
        case speak(String)
        /// Read it once listening ends.
        case deferred
        case skipped
    }

    /// A reply waits for listening to end; a sample or a replay asked for while listening is dropped.
    public mutating func say(_ text: String, listening: Bool, isReply: Bool) -> Talk {
        guard listening else { return .speak(text) }
        guard isReply else { return .skipped }
        deferredReply = text
        return .deferred
    }

    public enum Part: Equatable {
        /// The reply's first sentence: start speaking, cutting off anything still playing.
        case start(String)
        /// A later sentence: queue it after the ones already speaking.
        case add(String)
        /// Kept until listening ends; `note` is true for the first sentence kept.
        case deferred(note: Bool)
    }

    private var replyStarted = false

    /// A new spoken reply is about to stream in.
    public mutating func newReply() { replyStarted = false }

    /// One sentence of a reply that's still being written. Once a sentence has waited for listening to end, the rest
    /// of the reply waits too, so it's read in order.
    public mutating func part(_ sentence: String, listening: Bool) -> Part {
        if listening || deferredReply != nil {
            let first = deferredReply == nil
            deferredReply = [deferredReply, sentence].compactMap { $0 }.joined(separator: " ")
            return .deferred(note: first)
        }
        defer { replyStarted = true }
        return replyStarted ? .add(sentence) : .start(sentence)
    }

    /// The reply to read now that listening has ended.
    public mutating func listeningEnded() -> String? {
        defer { deferredReply = nil }
        return deferredReply
    }
}

public enum Transcription {
    /// `--transcribe` succeeds only with words: an error or empty text exits non-zero.
    public static func succeeded(_ text: String?) -> Bool {
        guard let text = text?.trimmingCharacters(in: .whitespacesAndNewlines) else { return false }
        return !text.isEmpty && !text.hasPrefix("error:")
    }
}
