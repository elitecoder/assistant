import Accelerate
import Foundation

/// Pipecat's Smart Turn v3.2 (BSD 2-Clause) decides from the sound of the speech whether the operator has finished
/// a turn, and Silero VAD v6.2.3 (MIT) says when they've stopped talking. Both run on this Mac with ONNX Runtime.
/// Files are pinned by revision and checked by SHA-256.
public enum SmartTurn {
    public static let files = [
        // The full-precision export ("gpu"); it runs on the CPU too. The 8-bit one flipped decisions on inputs that
        // differ by 0.00004, so near 0.5 it answered at random.
        KokoroFile(name: "smart-turn-v3.2-gpu.onnx",
                   url: URL(string: "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfdf7737ac64aad813d91bbfd56bf93/smart-turn-v3.2-gpu.onnx")!,
                   sha256: "ab8dc64b88713f90b571c15b714bd1330e6c883cad8763dacf65c9376dc539be"),
        KokoroFile(name: "silero_vad.onnx",
                   url: URL(string: "https://github.com/snakers4/silero-vad/raw/v6.2.3/src/silero_vad/data/silero_vad.onnx")!,
                   sha256: "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    ]

    public static var model: KokoroFile { files[0] }
    public static var vad: KokoroFile { files[1] }

    public static let sampleRate = 16_000
    /// The model hears the last 8 seconds of the turn.
    public static let window = 8 * sampleRate
    public static let melBins = 80
    public static let frames = 800
    static let fftSize = 400
    static let hop = 160
    static let bins = fftSize / 2 + 1

    /// The last 8 seconds of `samples`, with silence in front when there's less.
    public static func lastWindow(_ samples: [Float]) -> [Float] {
        samples.count >= window ? Array(samples.suffix(window))
            : [Float](repeating: 0, count: window - samples.count) + samples
    }

    /// Whisper's log-mel features for 8 seconds of 16 kHz audio, as Hugging Face's WhisperFeatureExtractor makes
    /// them with `chunk_length=8` and `do_normalize=True`: 80 mel bins by 800 frames, row-major.
    public static func features(_ window: [Float]) -> [Float] {
        precondition(window.count == Self.window)
        let mean = window.reduce(0, +) / Float(window.count)
        let variance = window.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / Float(window.count)
        let scale = 1 / (variance + 1e-7).squareRoot()
        let normalized = window.map { ($0 - mean) * scale }
        let padded = reflectPad(normalized, by: fftSize / 2)
        let frameCount = 1 + (padded.count - fftSize) / hop
        var framed = [Float](repeating: 0, count: frameCount * fftSize)
        for f in 0..<frameCount {
            for n in 0..<fftSize { framed[f * fftSize + n] = padded[f * hop + n] * hann[n] }
        }
        var real = [Float](repeating: 0, count: frameCount * bins)
        var imaginary = [Float](repeating: 0, count: frameCount * bins)
        vDSP_mmul(framed, 1, cosines, 1, &real, 1, vDSP_Length(frameCount), vDSP_Length(bins), vDSP_Length(fftSize))
        vDSP_mmul(framed, 1, sines, 1, &imaginary, 1, vDSP_Length(frameCount), vDSP_Length(bins), vDSP_Length(fftSize))
        var power = [Float](repeating: 0, count: frameCount * bins)
        for i in power.indices { power[i] = real[i] * real[i] + imaginary[i] * imaginary[i] }
        var mel = [Float](repeating: 0, count: frameCount * melBins)
        vDSP_mmul(power, 1, filters, 1, &mel, 1, vDSP_Length(frameCount), vDSP_Length(melBins), vDSP_Length(bins))
        var out = [Float](repeating: 0, count: melBins * frames)
        for m in 0..<melBins {
            for f in 0..<frames { out[m * frames + f] = log10(max(mel[f * melBins + m], 1e-10)) }
        }
        let ceiling = (out.max() ?? 0) - 8
        return out.map { (max($0, ceiling) + 4) / 4 }
    }

    static func reflectPad(_ x: [Float], by n: Int) -> [Float] {
        let front = (1...n).reversed().map { x[$0] }
        let back = (1...n).map { x[x.count - 1 - $0] }
        return front + x + back
    }

    /// A periodic Hann window, like NumPy's `hanning(401)[:-1]`.
    static let hann: [Float] = (0..<fftSize).map { 0.5 - 0.5 * cos(2 * Float.pi * Float($0) / Float(fftSize)) }

    /// The real DFT as two fftSize × bins matrices, row-major.
    static let cosines: [Float] = dft(cos)
    static let sines: [Float] = dft(sin)

    static func dft(_ f: (Double) -> Double) -> [Float] {
        var m = [Float](repeating: 0, count: fftSize * bins)
        for n in 0..<fftSize {
            for k in 0..<bins { m[n * bins + k] = Float(f(2 * Double.pi * Double(k * n) / Double(fftSize))) }
        }
        return m
    }

    /// Slaney-scale, Slaney-normalized mel filters from 0 to 8 kHz, bins × melBins row-major, as Hugging Face's
    /// `mel_filter_bank(norm="slaney", mel_scale="slaney")` builds them.
    static let filters: [Float] = {
        func mel(_ hz: Double) -> Double { hz < 1000 ? hz * 3 / 200 : 15 + log(hz / 1000) * 27 / log(6.4) }
        func hertz(_ m: Double) -> Double { m < 15 ? m * 200 / 3 : 1000 * exp((m - 15) * log(6.4) / 27) }
        let low = mel(0), high = mel(8000)
        let edges = (0..<(melBins + 2)).map { hertz(low + (high - low) * Double($0) / Double(melBins + 1)) }
        let fft = (0..<bins).map { 8000 * Double($0) / Double(bins - 1) }
        var m = [Float](repeating: 0, count: bins * melBins)
        for b in 0..<bins {
            for j in 0..<melBins {
                let down = (fft[b] - edges[j]) / (edges[j + 1] - edges[j])
                let up = (edges[j + 2] - fft[b]) / (edges[j + 2] - edges[j + 1])
                let weight = max(0, min(down, up)) * 2 / (edges[j + 2] - edges[j])
                m[b * melBins + j] = Float(weight)
            }
        }
        return m
    }()
}

/// When a spoken question is over. With Smart Turn: once the voice detector hears a pause after speech, ask the model
/// at 0.2, 0.5, and 0.9 seconds of silence, and end the turn only when it says the turn is complete (above 0.9) every
/// time. Once it says the operator is mid-sentence, keep listening, up to `limit` of silence. Without the model, the
/// old fixed timer. On 160 rendered questions (clean, and with brown and pink room noise at 25 and 15 dB), this
/// matched or beat the 2.5-second timer in every condition; a single call above 0.5 didn't, once there was noise.
public struct TurnEnd: Equatable {
    public enum Decision: Equatable {
        case wait
        /// Ask Smart Turn about the audio so far.
        case ask
        /// The turn is over; `byModel` is false when a timer ended it.
        case done(byModel: Bool)
    }

    public static let asks: [TimeInterval] = [0.2, 0.5, 0.9]
    public static let threshold: Float = 0.9
    public static let limit: TimeInterval = 3
    public static let timer: TimeInterval = 2.5

    /// The Listener's own timer after words: with Smart Turn deciding it's only a backstop past the silence limit.
    public static func backstop(timer: TimeInterval, smartTurn: Bool) -> TimeInterval {
        smartTurn ? max(timer, limit + 1) : timer
    }

    public let modelAvailable: Bool
    public private(set) var heardSpeech = false
    public private(set) var silence: TimeInterval = 0
    /// Calls in this pause that said the turn is complete.
    private var agreed = 0
    /// An ask is out and its answer hasn't come.
    private var pending = false
    /// A call in this pause said the operator is mid-sentence.
    private var doubted = false

    public init(modelAvailable: Bool) { self.modelAvailable = modelAvailable }

    /// One voice-detector window: speech or not, and how long it was.
    public mutating func take(speech: Bool, duration: TimeInterval) -> Decision {
        if speech {
            heardSpeech = true
            silence = 0
            agreed = 0
            pending = false
            doubted = false
            return .wait
        }
        guard heardSpeech else { return .wait }
        silence += duration
        if !modelAvailable { return silence >= Self.timer ? .done(byModel: false) : .wait }
        if silence >= Self.limit { return .done(byModel: false) }
        if !doubted && !pending && agreed < Self.asks.count && silence >= Self.asks[agreed] {
            pending = true
            return .ask
        }
        return .wait
    }

    /// Smart Turn's probability that the turn is complete, for the audio up to the last `.ask`. An answer that arrives
    /// after the operator spoke again is stale and ignored.
    public mutating func answer(_ probability: Float) -> Decision {
        guard pending else { return .wait }
        pending = false
        guard probability > Self.threshold else {
            doubted = true
            return .wait
        }
        agreed += 1
        return agreed == Self.asks.count ? .done(byModel: true) : .wait
    }
}
