// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CcshiftMenuBar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "CcshiftMenuBar", targets: ["CcshiftMenuBar"]),
    ],
    targets: [
        .executableTarget(name: "CcshiftMenuBar"),
        .testTarget(name: "CcshiftMenuBarTests", dependencies: ["CcshiftMenuBar"]),
    ]
)
