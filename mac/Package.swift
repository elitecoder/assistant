// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AssistantBar",
    platforms: [.macOS(.v14)],
    targets: [
        .target(name: "SessionCore"),
        .executableTarget(name: "AssistantBar", dependencies: ["SessionCore"]),
        .testTarget(name: "SessionCoreTests", dependencies: ["SessionCore"]),
        .testTarget(name: "AssistantBarTests", dependencies: ["AssistantBar", "SessionCore"]),
    ],
    swiftLanguageModes: [.v5]
)
