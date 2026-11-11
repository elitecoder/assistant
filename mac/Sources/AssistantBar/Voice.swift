import AVFoundation
import SessionCore
import Speech

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
final class Listener: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    var onHeard: ((String) -> Void)?
    var onProblem: ((String) -> Void)?

    private let engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var pause: Timer?

    func toggle() { listening ? stop() : start() }

    func start() {
        AVCaptureDevice.requestAccess(for: .audio) { mic in
            SFSpeechRecognizer.requestAuthorization { speech in
                DispatchQueue.main.async {
                    if !mic {
                        self.onProblem?("Assistant can't use the microphone. Allow it in System Settings, Privacy & Security, Microphone.")
                    } else if speech != .authorized {
                        self.onProblem?("Assistant can't use speech recognition. Allow it in System Settings, Privacy & Security, Speech Recognition.")
                    } else {
                        self.begin()
                    }
                }
            }
        }
    }

    private func begin() {
        guard !listening, let recognizer, recognizer.isAvailable else {
            onProblem?("Speech recognition isn't available right now.")
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 1024, format: input.outputFormat(forBus: 0)) { buffer, _ in
            request.append(buffer)
        }
        engine.prepare()
        do { try engine.start() } catch {
            input.removeTap(onBus: 0)
            onProblem?("The microphone didn't start: \(error.localizedDescription)")
            return
        }
        self.request = request
        heard = ""
        listening = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.listening else { return }
                if let result {
                    self.heard = result.bestTranscription.formattedString
                    self.waitForPause()
                    if result.isFinal { self.stop() }
                } else if error != nil {
                    self.stop()
                }
            }
        }
        waitForPause()
    }

    private func waitForPause() {
        pause?.invalidate()
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? 6 : 1.8, repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    func stop() {
        guard listening else { return }
        listening = false
        pause?.invalidate()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { onHeard?(text) }
    }
}

final class Speaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var speaking = false
    private let synth = AVSpeechSynthesizer()

    override init() {
        super.init()
        synth.delegate = self
    }

    func say(_ markdown: String) {
        synth.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: Spoken.plain(markdown))
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        speaking = true
        synth.speak(utterance)
    }

    func stop() {
        synth.stopSpeaking(at: .immediate)
        speaking = false
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = false }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = false }
    }
}
