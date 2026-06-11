// Intentionally minimal. Plugin versions are pinned once in gradle/libs.versions.toml
// and applied per-module via catalog aliases, so a pure-Kotlin `:core` build never
// resolves the Android Gradle Plugin (which lives only on Google's Maven). This is what
// lets `./gradlew :core:test` run in a sandbox without the Android SDK; CI applies the
// Android plugins in `:app` where the SDK is present.

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
