// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "CcsMenuBar",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "CcsMenuBar", targets: ["CcsMenuBar"]),
    ],
    targets: [
        .executableTarget(name: "CcsMenuBar"),
        .testTarget(name: "CcsMenuBarTests", dependencies: ["CcsMenuBar"]),
    ]
)
