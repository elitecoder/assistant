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
