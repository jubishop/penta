// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "PentaMac",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .library(name: "PentaCore", targets: ["PentaCore"]),
        .executable(name: "PentaMac", targets: ["PentaMac"])
    ],
    dependencies: [
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "7.10.0")
    ],
    targets: [
        .target(
            name: "PentaCore",
            dependencies: [
                .product(name: "GRDB", package: "GRDB.swift")
            ]
        ),
        .executableTarget(
            name: "PentaMac",
            dependencies: ["PentaCore"]
        ),
        .testTarget(
            name: "PentaCoreTests",
            dependencies: ["PentaCore"]
        )
    ]
)
