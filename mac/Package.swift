// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AssistantBar",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(url: "https://github.com/mlalma/kokoro-ios.git", exact: "1.0.11"),
        .package(url: "https://github.com/mlalma/MLXUtilsLibrary.git", exact: "0.0.6"),
        .package(url: "https://github.com/ml-explore/mlx-swift", exact: "0.30.2"),
        .package(url: "https://github.com/microsoft/onnxruntime-swift-package-manager", exact: "1.24.2"),
    ],
    targets: [
        .target(name: "SessionCore"),
        .target(name: "ObjCExceptions"),
        .executableTarget(name: "AssistantBar", dependencies: [
            "SessionCore",
            "ObjCExceptions",
            .product(name: "KokoroSwift", package: "kokoro-ios"),
            .product(name: "MLXUtilsLibrary", package: "MLXUtilsLibrary"),
            .product(name: "MLX", package: "mlx-swift"),
            .product(name: "onnxruntime", package: "onnxruntime-swift-package-manager"),
        ]),
        .testTarget(name: "SessionCoreTests", dependencies: ["SessionCore"]),
        .testTarget(name: "AssistantBarTests", dependencies: ["AssistantBar", "SessionCore", "ObjCExceptions"]),
    ],
    swiftLanguageModes: [.v5]
)
