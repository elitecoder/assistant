import Foundation
import XCTest
@testable import SessionCore

final class KokoroTests: XCTestCase {
    func testSentencesSplitForAFastStart() {
        XCTAssertEqual(Kokoro.sentences("Workspace 25 waits on you. It needs one choice! Which account?"),
                       ["Workspace 25 waits on you.", "It needs one choice!", "Which account?"])
    }

    func testHalvesCutAtTheClauseNearestTheMiddle() {
        XCTAssertEqual(Kokoro.halves("first part here, second part here"), ["first part here,", "second part here"])
        XCTAssertEqual(Kokoro.halves("one two three four"), ["one two", "three four"])
    }

    func testVoiceIDsMapToVoicePackKeys() {
        XCTAssertEqual(Kokoro.npzKey("kokoro.af_heart"), "af_heart.npy")
        XCTAssertNil(Kokoro.npzKey("com.apple.voice.compact.en-US.Samantha"))
    }

    func testChecksumMatchesKnownValue() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try Data("kokoro".utf8).write(to: url)
        XCTAssertEqual(try Kokoro.sha256(of: url), "1791019474d9bf5395919bfeda11bf46fd60579a5b6b43e6e4495731a397dc7d")
    }

    func testKokoroHeartIsTheDefaultVoice() {
        let samantha = VoiceInfo(id: "com.apple.voice.compact.en-US.Samantha", name: "Samantha", language: "en-US",
                                 quality: 1, novelty: false)
        XCTAssertEqual(Voices.choose(Kokoro.voices + [samantha], preferred: nil, systemDefault: samantha.id)?.name,
                       "Kokoro Heart")
    }
}

final class KokoroWordsTests: XCTestCase {
    func testNumbersAndSymbolsBecomeWords() {
        XCTAssertEqual(Kokoro.wordsForSpeech("Workspace 25 uses 60 GB, 45% of memory."),
                       "Workspace twenty five uses sixty GB, forty five percent of memory.")
        XCTAssertEqual(Kokoro.wordsForSpeech("It cost $0.24, then $3, then $1.50."),
                       "It cost twenty four cents, then three dollars, then one dollar and fifty cents.")
        XCTAssertEqual(Kokoro.wordsForSpeech("Version 1.0.11 and 2.5 seconds on PR #56."),
                       "Version one point zero point eleven and two point five seconds on PR number fifty six.")
        XCTAssertEqual(Kokoro.wordsForSpeech("1,200 files and/or more"), "one thousand two hundred files and or more")
    }

    func testLongSentencesSplitAtCommas() {
        let long = "Start with Remove Me As Work Bottleneck, workspace 3, because the model there is using about 60 gigabytes of memory."
        XCTAssertEqual(Kokoro.chunks(long), [
            "Start with Remove Me As Work Bottleneck, workspace three,",
            "because the model there is using about sixty gigabytes of memory.",
        ])
        XCTAssertEqual(Kokoro.chunks("Yes. Which account?"), ["Yes.", "Which account?"])
    }
}
