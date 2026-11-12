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
    static let common: Set<String> = ["the", "and", "for", "with", "from", "into", "that", "this", "fix", "add", "new",
                                      "not", "are", "was", "but", "all", "use", "via", "out", "off", "its"]

    /// The fixed terms, then each workspace title, then the distinctive words in those titles, without repeats
    /// (ignoring case) and at most `limit` of them.
    public static func contextualStrings(titles: [String], limit: Int = Vocabulary.limit) -> [String] {
        let cleaned = titles.map { $0.split(whereSeparator: \.isWhitespace).joined(separator: " ") }.filter { !$0.isEmpty }
        let words = cleaned.flatMap { title in
            title.split(whereSeparator: { !$0.isLetter && !$0.isNumber && $0 != "-" && $0 != "'" })
                .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: "-'")) }
                .filter { $0.count >= 3 && !$0.allSatisfy(\.isNumber) && !common.contains($0.lowercased()) }
        }
        var seen = Set<String>()
        var out: [String] = []
        for phrase in terms + cleaned + words where out.count < limit && seen.insert(phrase.lowercased()).inserted {
            out.append(phrase)
        }
        return out
    }
}

public enum Replay {
    /// The answer "Play again" speaks: the newest assistant answer with any text.
    public static func target(_ chat: [ChatTurn]) -> String? {
        chat.last { $0.role == .assistant && !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }?.text
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
        return "Downloading Apple's speech model (\(Int((fraction * 100).rounded()))%). Until it's ready, I use the older recognizer."
    }
}
