import Foundation

/// Decides when the operator has started talking over a spoken answer. It watches the echo-cancelled microphone and
/// fires once the voice has held, above the room's noise, for long enough: a cough or an "mm-hmm" is too short, and
/// what's left of Assistant's own voice after echo cancellation is too quiet.
public struct BargeInGate: Equatable {
    /// Voice this long, with no gap longer than `maximumGap`, interrupts.
    public var minimumVoiced: TimeInterval = 0.45
    public var maximumGap: TimeInterval = 0.15
    /// Echo cancellation needs a moment to adapt once playback starts; nothing interrupts before this.
    public var settle: TimeInterval = 0.3
    /// The quietest level that counts as voice (on Meter's 0–1 scale, about -38 dB), and how far above the room's
    /// noise it has to be.
    public var absoluteFloor: Float = 0.3
    public var margin: Float = 0.15

    private var noise: Float?
    private var elapsed: TimeInterval = 0
    private var voiced: TimeInterval = 0
    private var gap: TimeInterval = 0
    public private(set) var triggered = false

    /// `noise` is the room's level before playback started, when it's known.
    public init(noise: Float? = nil) { self.noise = noise }

    /// Takes one buffer's level; true exactly once, when the operator is talking over the answer.
    public mutating func take(level: Float, duration: TimeInterval) -> Bool {
        guard level.isFinite, duration > 0, !triggered else { return false }
        let start = elapsed
        elapsed += duration
        if let current = noise {
            noise = level < current ? level : current + (level - current) * 0.01
        } else {
            noise = level
        }
        guard start >= settle else { return false }
        if level >= max(absoluteFloor, (noise ?? 0) + margin) {
            voiced += duration
            gap = 0
        } else {
            gap += duration
            if gap > maximumGap { voiced = 0 }
        }
        if voiced >= minimumVoiced {
            triggered = true
            return true
        }
        return false
    }
}

/// The barge-in gate, armed only while Assistant's audio is actually playing. Between answers, between sentences, and
/// while Claude is still writing, it starts over, so noise then can't use it up, and its settle time starts when
/// playback does, when echo cancellation is adapting and echo leaks most. While nothing plays it keeps a rolling level
/// of the room, about the last second, and each playback's gate starts from it instead of from its first buffer, which
/// may already hold the answer's own echo or the operator's voice.
public struct BargeInWatch: Equatable {
    private var gate = BargeInGate()
    public private(set) var room: Float?
    /// How long the rolling room level remembers.
    static let roomMemory: TimeInterval = 1

    public init() {}

    public mutating func take(level: Float, duration: TimeInterval, playing: Bool) -> Bool {
        guard playing else {
            if level.isFinite {
                room = room.map { $0 + (level - $0) * Float(min(1, duration / Self.roomMemory)) } ?? level
            }
            gate = BargeInGate(noise: room)
            return false
        }
        return gate.take(level: level, duration: duration)
    }
}

/// What the echo-cancelled microphone's tap decides about each buffer: whether the operator is talking over the
/// answer, and whether the microphone has sent nothing at all (every sample exactly zero) for `TalkModeNote.silentAfter`.
/// A quiet room under voice processing reads as the meter's floor, not as zeros, so it doesn't count as no sound.
public struct TalkTap: Equatable {
    public private(set) var watch = BargeInWatch()
    private var zeroFor: TimeInterval = 0

    public init() {}

    public var silent: Bool { zeroFor >= TalkModeNote.silentAfter }

    /// `allZero` is true when every sample in the buffer is exactly zero. Returns whether the operator barged in,
    /// and the new value of `silent` when it changed.
    public mutating func take(level: Float, allZero: Bool, duration: TimeInterval, playing: Bool) -> (bargeIn: Bool, silent: Bool?) {
        let was = silent
        zeroFor = allZero ? zeroFor + duration : 0
        return (watch.take(level: level, duration: duration, playing: playing), silent != was ? silent : nil)
    }
}

/// Talk Mode's loop: listen, answer, speak, listen again. While an answer is spoken the microphone stays open
/// (echo-cancelled) only to notice the operator talking over it; nothing is transcribed until the answer stops.
public struct TalkLoop: Equatable {
    public enum Phase: Equatable {
        case off
        case listening
        case answering
        case speaking
    }

    public enum Action: Equatable {
        /// Start listening for the next question.
        case listen
        /// Stop the answer that's playing, then listen.
        case interrupt
        /// Open the echo-cancelled microphone that watches for the operator talking over the answer.
        case watch
        case stopWatching
        /// Talk Mode ended; show `note` when there is one.
        case exit(note: String?)
    }

    public private(set) var phase = Phase.off

    public static let nothingHeardNote = "Talk Mode is off: I didn't hear a question."
    public static let micLostNote = "Talk Mode is off: the microphone disconnected."
    /// Stored as the reply of a run that ended because the operator talked over it.
    public static let interruptedReason = "Interrupted by voice."
    public static let longPress: TimeInterval = 0.6

    /// Holding Control-Option-V this long turns on Talk Mode.
    public static func isLongPress(held: TimeInterval) -> Bool { held >= longPress }

    public init() {}

    public var on: Bool { phase != .off }

    public mutating func toggle() -> [Action] {
        if on { return stop() }
        phase = .listening
        return [.listen]
    }

    /// A question was heard and is being asked.
    public mutating func heard() -> [Action] {
        guard phase == .listening else { return [] }
        phase = .answering
        return [.watch]
    }

    /// Listening ended without a question (silence, an error, or no words).
    public mutating func nothingHeard() -> [Action] {
        guard phase == .listening else { return [] }
        phase = .off
        return [.exit(note: Self.nothingHeardNote)]
    }

    public mutating func startedSpeaking() -> [Action] {
        if phase == .answering { phase = .speaking }
        return []
    }

    /// The answer is done playing, or there was nothing to play.
    public mutating func finishedSpeaking() -> [Action] {
        guard phase == .speaking || phase == .answering else { return [] }
        phase = .listening
        return [.stopWatching, .listen]
    }

    /// The operator talked over the answer. Only an answer that's playing can be interrupted.
    public mutating func bargedIn() -> [Action] {
        guard phase == .speaking else { return [] }
        phase = .listening
        return [.stopWatching, .interrupt]
    }

    /// The operator started a question by key during an answer. The question has the microphone, so the watch for
    /// interruptions closes (other apps stop being ducked, and only one engine holds a microphone); the next question
    /// heard opens it again for its answer.
    public mutating func listenedByKey() -> [Action] {
        guard phase == .answering || phase == .speaking else { return [] }
        phase = .listening
        return [.stopWatching]
    }

    /// The microphone went away mid-question: nothing is listening, so Talk Mode ends.
    public mutating func micLost() -> [Action] {
        guard on else { return [] }
        phase = .off
        return [.stopWatching, .exit(note: Self.micLostNote)]
    }

    /// The Stop button or Esc.
    public mutating func stop() -> [Action] {
        guard on else { return [] }
        phase = .off
        return [.stopWatching, .exit(note: nil)]
    }
}

/// What the panel says about the microphone Talk Mode watches for interruptions: voice processing always listens
/// on the Mac's default input, which may not be the microphone picked for questions, or may send nothing.
public enum TalkModeNote {
    public static func text(defaultInput: String?, picked: String?, silent: Bool) -> String? {
        let name = defaultInput ?? "the default microphone"
        if silent { return "Talking over an answer won't stop it: \(name), your Mac's default microphone, sends no sound." }
        if let picked, picked != defaultInput {
            return "Talking over an answer listens on \(name), your Mac's default microphone, not \(picked)."
        }
        return nil
    }

    /// Silence for this long from the echo-cancelled microphone means it sends nothing at all.
    public static let silentAfter: TimeInterval = 2
}
