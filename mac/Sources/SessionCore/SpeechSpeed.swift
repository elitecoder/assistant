import Foundation

/// Talkback speed: a multiplier the operator picks, applied to Kokoro and to Apple's voices.
public enum SpeechSpeed {
    public static let choices: [Double] = [1.0, 1.25, 1.5, 1.75, 2.0]
    public static let defaultValue = 1.0

    public static func clamped(_ value: Double) -> Double {
        value.isFinite ? min(2.0, max(1.0, value)) : defaultValue
    }

    /// Apple's utterance rate for a multiplier: the default rate scaled, capped at the maximum rate.
    public static func appleRate(_ multiplier: Double, defaultRate: Float, maximumRate: Float) -> Float {
        min(maximumRate, defaultRate * Float(clamped(multiplier)))
    }

    public static func label(_ value: Double) -> String {
        let text = String(format: "%.2f", clamped(value))
            .replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression)
        return text + "×"
    }
}
