pluginManagement {
    repositories {
        // Maven Central first so the pure-Kotlin :core never reaches for Google's
        // Maven (which some sandboxes block); the Android plugins live on google().
        gradlePluginPortal()
        mavenCentral()
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        mavenCentral()
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
    }
}

rootProject.name = "mcctl-android"

// :core is pure Kotlin/JVM (Maven Central only) — it builds and tests anywhere,
// even on a box without the Android SDK. :app is the Compose UI and needs the
// SDK + Google Maven, so it is included only when an SDK is actually configured.
// This keeps `./gradlew :core:test` working in a locked-down sandbox while CI
// (with the SDK installed) builds the full APK.
include(":core")

val androidSdkAvailable =
    System.getenv("ANDROID_HOME") != null ||
    System.getenv("ANDROID_SDK_ROOT") != null ||
    file("local.properties").let { it.exists() && it.readText().contains("sdk.dir") }

if (androidSdkAvailable) {
    include(":app")
} else {
    gradle.startParameter.let {
        logger.lifecycle(
            "[mcctl-android] No Android SDK detected — configuring :core only. " +
            "Install the SDK (or set ANDROID_HOME) to build the :app APK."
        )
    }
}
